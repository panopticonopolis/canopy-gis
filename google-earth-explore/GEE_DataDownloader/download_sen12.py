import ee
import yaml
import time
import os
import json
from argparse import ArgumentParser
from utils import clipToROI, exportImageCollectionToGCS, exportImageToGCS, sentinel2CloudScore, calcCloudCoverage, inject_B10
from utils import GEETaskManager

from gevent.fileobject import FileObjectThread

def makeFilterList(sensor):
	filters_before = None
	filters_after = None

	def _build_filters(filter_list):
		filters = []
		for f in filter_list:
			key = list(f.keys())[0]
			op = list(list(f.values())[0].keys())[0]
			val = list(list(f.values())[0].values())[0]
			filters.append(getattr(ee.Filter, op)(key, val))

		return filters

	if 'filters_before' in sensor:
		filters_before = _build_filters(sensor['filters_before'])

	if 'filters_after' in sensor:
		filters_after = _build_filters(sensor['filters_after'])

	return filters_before, filters_after

def makeImageCollection(sensor, roi, start_date, end_date, modifiers=[]):
	filters_before, filters_after = makeFilterList(sensor)

	collection = ee.ImageCollection(sensor['name']) \
				.filterDate(ee.Date(start_date), ee.Date(end_date)) \
				.filterBounds(roi) \
				.map( lambda x: clipToROI(x, ee.Geometry(roi)) ) \
				.map(inject_B10)

	if filters_before is not None:
		collection = collection.filter( filters_before )

	if modifiers and len(modifiers) > 0:
		for m in modifiers:
			collection = collection.map(m)

	if filters_after:
		collection = collection.filter( filters_after )

	return collection.select(sensor['bands'])

def process_datasource(task_queue, source, sensor, export_to, export_dest, feature_list = None):
# 	feature_list = ee.FeatureCollection(source['features_src'])
	feature_list = feature_list.sort(source['sort_by']).toList(feature_list.size())
	n_features = feature_list.size().getInfo()

	print("{} features have been loaded".format(n_features))

	task_list = []

	### ERROR? ###
	## Originally this was range(1, n_features), but we're pretty sure
	## that should be 0 so we changed it.
	for i in range(0, n_features):
		feature_point = ee.Feature( feature_list.get(i) )

		if source['geometry'] == "point":
			feature_point = feature_point.buffer(source['size']).bounds()

		roi = feature_point.geometry()
		roi = roi.coordinates().getInfo()

		### should be done outside the for loop ###
		if isinstance(source['name'], str):
			source['name'] = [source['name']]

		### ERROR? ###
		## The following conditional should be moved under
		## the conditional after it, or else we'll error out
		## if sensor doesn't have a "prefix" key.
		if isinstance(sensor['prefix'], str):
			sensor['prefix'] = [sensor['prefix']]

		if 'prefix' in sensor:
			filename_parts = sensor['prefix'] + source['name']
		else:
			filename_parts = source['name']
		### end of part that should be done outside the for loop ###

		filename = "_".join(source['name'] + [str(i)])
		dest_path = "/".join(filename_parts + [filename])

		export_params = {
			'bucket': export_dest,
			'resolution': source['resolution'],
			'filename': filename,
			'dest_path': dest_path
		}

		task_params = {
			'action': export_single_feature,
			'id': "_".join(filename_parts + [str(i)]), # This must be unique per task, to allow to track retries
			'kwargs': {
				'roi': roi,
				'export_params': export_params,
				'sensor': sensor,
				'date_range': {'start_date': source['start_date'], 'end_date': source['end_date']}
			}
		}

		task_queue.add_task(task_params, blocking=True)

def export_single_feature(roi=None, sensor=None, date_range=None, export_params=None):
	modifiers = None
	if sensor['type'].lower() == "opt":
		#print(sensor['type'])
		modifiers = [sentinel2CloudScore, calcCloudCoverage]

	roi_ee = ee.Geometry.Polygon(roi[0])
	image_collection = makeImageCollection(sensor, roi_ee, date_range['start_date'], date_range['end_date'], modifiers=modifiers)
	img = image_collection.mosaic().clip(roi_ee)
	#print('Mosaic type:', type(img))

	new_params = export_params.copy()
	new_params['img'] = img
	new_params['roi'] = roi

	return exportImageToGCS(**new_params)

def _serialise_task_log(task_log):
	for k,v in task_log.items():
		task_log[k]['task_def']['action'] = "export_single_feature"

	return task_log

def load_task_log(filename='task_log.json'):
	with open(filename, 'r') as f:
		task_log = json.load(f)

	for k, v in task_log.items():
		task_log[k]['task_def']['action'] = globals()[task_log[k]['task_def']['action']]

	return task_log

def monitor_tasks(task_log):
	print("SAVING LOG")
	f_raw = open('task_log.json', 'w')
	with FileObjectThread(f_raw, 'w') as handle:
		task_log = _serialise_task_log(task_log)
		json.dump(task_log, handle)

	f_raw.close()

def load_config(path):
	with open(path, 'r') as stream:
		try:
			return yaml.load(stream)
		except yaml.YAMLError as exc:
			print(exc)

if __name__ == "__main__":
	parser = ArgumentParser()
	parser.add_argument("-c", "--config", default=None, help="Config file for the download")
	args = parser.parse_args()

	assert args.config, "Please specify a config file for the download"
	config = load_config(args.config)
	print(config)

	ee.Initialize()

	task_queue = GEETaskManager(n_workers=config['max_tasks'], max_retry=config['max_retry'], wake_on_task=True, log_file=config['log_file'], process_timeout=config['task_timeout'])
	task_queue.register_monitor(monitor_tasks)

	if os.path.exists('task_log.json'):
		task_log = load_task_log(filename='task_log.json')
		task_queue.set_task_log(task_log)

	for data_list in config['data_list']:
		for sensor_idx in data_list['sensors']:
			sensor = config['sensors'][sensor_idx]
			tasks = process_datasource(task_queue, data_list, sensor, config['export_to'], config['export_dest'])

	print("Waiting for completion...")
	task_queue.wait_till_done()

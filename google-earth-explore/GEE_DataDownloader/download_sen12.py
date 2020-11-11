import ee
import yaml
import time
import os
import json
import time
from argparse import ArgumentParser
from utils import clipToROI, exportImageCollectionToGCS, exportImageToGCS, sentinel2CloudScore, calcCloudCoverage, inject_B10, sentinel2ProjectShadows, computeQualityScore, mergeCollection
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
				.map( lambda x: clipToROI(x, ee.Geometry(roi)) )
    
# 	print(collection.getInfo())

	if filters_before is not None:
		collection = collection.filter( filters_before )

	if modifiers and len(modifiers) > 0:
		for m in modifiers:
			#print(f'Applying modifier {m}')
			collection = collection.map(m)

	if filters_after:
		collection = collection.filter( filters_after )

	return collection

def process_datasource(source, sensor, export_folder, feature_list = None, pre_mosaic_sort='CLOUDY_PERCENTAGE'):
# 	feature_list = ee.FeatureCollection(source['features_src'])
	feature_list = feature_list.sort(source['sort_by']).toList(feature_list.size())
	n_features = feature_list.size().getInfo()

	print("{} features have been loaded".format(n_features))

	#task_list = []

	exports = []

	### ERROR? ###
	## Originally this was range(1, n_features), but we're pretty sure
	## that should be 0 so we changed it.
	for i in range(0, n_features):
		feature_point = ee.Feature( feature_list.get(i) )

		#if source['geometry'] == "point":
		#	feature_point = feature_point.buffer(source['size']).bounds()

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

		time_stamp = "_".join(time.ctime().split(" ")[1:])
		filename = "_".join(source['name'] + [str(i)] + [time_stamp])
		dest_path = "/".join(filename_parts + [filename])

		export_params = {
			'bucket': export_folder,
			'resolution': source['resolution'],
			'filename': filename,
			'dest_path': dest_path
		}

		# task_params = {
		# 	'action': export_single_feature,
		# 	'id': "_".join(filename_parts + [str(i)]), # This must be unique per task, to allow to track retries
		# 	'kwargs': {
		# 		'roi': roi,
		# 		'export_params': export_params,
		# 		'sensor': sensor,
		# 		'date_range': {'start_date': source['start_date'], 'end_date': source['end_date'],
		# 		'sort_by': pre_mosaic_sort}
		# 	}
		# }

		# task_queue.add_task(task_params, blocking=True)

		export = export_single_feature(
			roi=roi,
			sensor=sensor,
			date_range={'start_date': source['start_date'], 'end_date': source['end_date']},
			export_params=export_params,
			sort_by=pre_mosaic_sort
		)

		exports.append(export)

	return exports

def export_single_feature(roi=None, sensor=None, date_range=None, export_params=None, sort_by='CLOUDY_PERCENTAGE'):
	modifiers = []
	if sensor['name'].lower() == "copernicus/s2_sr":
		print('Inject B10')
		modifiers.append(inject_B10)
	if sensor['type'].lower() == "opt":
		#print(sensor['type'])
		modifiers += [sentinel2CloudScore, calcCloudCoverage, sentinel2ProjectShadows, computeQualityScore]

	#print('Modifiers:', modifiers)
	roi_ee = ee.Geometry.Polygon(roi[0])
	image_collection = makeImageCollection(sensor, roi_ee, date_range['start_date'], date_range['end_date'], modifiers=modifiers)
	## sort was not in the original version
	image_collection = image_collection.sort(sort_by)
	## below line was in the original verson;
	## changing to the JS version
	## img = image_collection.mosaic().clip(roi_ee)
	cloudFree = mergeCollection(image_collection).clip(roi_ee)
	cloudFree = cloudFree.reproject('EPSG:4326', None, 10)
	### Do we need to mosaic it now???
	print('cloudFree info:', cloudFree.getInfo())
	#print('Mosaic type:', type(img))

	new_params = export_params.copy()
	new_params['img'] = cloudFree
	new_params['roi'] = roi
	new_params['sensor_name'] = sensor['name'].lower()

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

# def load_config(path):
# 	with open(path, 'r') as stream:
# 		try:
# 			return yaml.load(stream)
# 		except yaml.YAMLError as exc:
# 			print(exc)

def load_config(config_file):
    stream = open(config_file, 'r') 
    return yaml.load(stream)

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

	pre_mosaic_sort = config['pre_mosaic_sort']
	for data_list in config['data_list']:
		for sensor_idx in data_list['sensors']:
			sensor = config['sensors'][sensor_idx]
			tasks = process_datasource(task_queue, data_list, sensor, config['export_to'], config['export_dest'], pre_mosaic_sort)

	print("Waiting for completion...")
	task_queue.wait_till_done()

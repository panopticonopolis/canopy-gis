import ee
import yaml
import time
import os
import json
import time
from pandas.tseries.offsets import DateOffset
from argparse import ArgumentParser
from utils import clipToROI, exportImageCollectionToGCS, exportImageToGCS, sentinel2CloudScore, calcCloudCoverage, inject_B10, sentinel2ProjectShadows, computeQualityScore, mergeCollection
from utils import GEETaskManager
from utils import collection_greater_than

from gevent.fileobject import FileObjectThread
import logging


LOG_FILENAME = 'full_basin_export_history.log'
logging.basicConfig(filename=LOG_FILENAME,level=logging.INFO)


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

def makeImageCollection(sensor, roi, start_date, end_date, modifiers=[], tile=None):
	filters_before, filters_after = makeFilterList(sensor)
	#print(modifiers)

	collection = ee.ImageCollection(sensor['name']) \
				.filterDate(ee.Date(start_date), ee.Date(end_date)) \
				.filterBounds(roi)

	if tile is None:
		#print('clipping in makeImageCollection')
		## If we're doing a feature collection
		collection = collection.map( lambda x: clipToROI(x, ee.Geometry(roi)) )
	else:
		#print('not clipping in makeImageCollection')
		## If we're doing it by tile
		collection = collection.filterMetadata(
			'system:index', 'contains', tile
		)
    
	#print("size of collection:",collection.size().getInfo())

	if filters_before is not None:
		collection = collection.filter( filters_before )

	if modifiers and len(modifiers) > 0:
		for m in modifiers:
			#print(f'Applying modifier {m}')
			collection = collection.map(m)

	if filters_after:
		collection = collection.filter( filters_after )

	return collection

def process_datasource(source, sensor, export_folder, feature_list = None, pre_mosaic_sort='CLOUDY_PIXEL_PERCENTAGE'):
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
		polygon_id = i+1

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
		filename = "_".join([str(i + 1)] + source['name'] + [time_stamp])
		print("processing ",filename)
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

		date_range = {
			'start_date': source['start_date'],
			'end_date': source['end_date'],
			'original_date': '<preset date>',
			'day_offset': '<preset date>',
			'area': '<preset date>'
		}

		export = export_single_feature(
			roi=roi,
			sensor=sensor,
			date_range=date_range,
			export_params=export_params,
			sort_by=pre_mosaic_sort,
			polygon_id=polygon_id,
			skip_test=True
		)

		exports.append(export)

	return exports

def process_datasource_custom_daterange(
	source, sensor, export_folder, polygons, offset_dict,
	date_range_list=[], pre_mosaic_sort='CLOUDY_PIXEL_PERCENTAGE',
	area_limit=1000, loop_start=0, limit=None, minutes_to_wait=60, debug=False,
	no_dynamic_daterage=False
):
# 	feature_list = ee.FeatureCollection(source['features_src'])
	if type(polygons) is dict:
		polygons_are_tiles = True
		tile_list = list(polygons.keys())
		n_polygons = len(tile_list)
		print(f'{n_polygons} tiles have been loaded')

	elif type(polygons) is ee.featurecollection.FeatureCollection:
		polygons_are_tiles = False
		feature_list = polygons.sort(source['sort_by']).toList(polygons.size())
		n_polygons = feature_list.size().getInfo()
		print(f"{n_polygons} features have been loaded")

	else:
		raise ValueError('"polygons" must be a hash table or a feature collection')

	#task_list = []

	exports = []
	exceptions = []

	if isinstance(source['name'], str):
		source['name'] = [source['name']]

	if 'prefix' in sensor:
		if isinstance(sensor['prefix'], str):
			sensor['prefix'] = [sensor['prefix']]
		filename_parts = sensor['prefix'] + source['name']
	else:
		filename_parts = source['name']

	if not limit:
		limit = n_polygons

	### ERROR? ###
	## Originally this was range(1, n_features), but we're pretty sure
	## that should be 0 so we changed it.
	for i in range(loop_start, limit):
		polygon_id = i + 1

		print(f'processing polygon {polygon_id} of {limit}', end='\r', flush=True)

		if polygons_are_tiles:
			tile = tile_list[i]
			roi = json.loads(polygons[tile]['Polygon'])
		else:
			feature_point = ee.Feature( feature_list.get(i) )
			roi = feature_point.geometry()
			roi = roi.coordinates().getInfo()[0]
			tile = None

		#if source['geometry'] == "point":
		#	feature_point = feature_point.buffer(source['size']).bounds()

		time_stamp = "_".join(time.ctime().split(" ")[1:])
		filename = "_".join([str(polygon_id)] + source['name'] + [time_stamp])
		# print("processing ",filename)
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

		date_range = date_range_list[i]

		params = {
			'roi': roi,
			'sensor': sensor,
			'date_range': date_range,
			'export_params': export_params,
			'sort_by': pre_mosaic_sort,
			'polygon_id': polygon_id,
			'area_limit': area_limit,
			'skip_test': no_dynamic_daterage,
			'tile': tile,
			'offset_dict': offset_dict
		}

		# export = export_single_feature(
		# 	roi=roi,
		# 	sensor=sensor,
		# 	date_range=date_range,
		# 	export_params=export_params,
		# 	sort_by=pre_mosaic_sort,
		# 	polygon_id=polygon_id,
		# 	area_limit=area_limit,
		# 	skip_test=False,
		# 	tile=tile
		# )

		# exports.append(export)

		export_try_except_loop(params, minutes_to_wait, exports, exceptions, 0, debug)
		# print(f'Polygon {polygon_id} processed; please wait')
		# time.sleep(30)
		if debug:
			logging_timestamp = "_".join(time.ctime().split(" ")[1:])
			logging.info(f'{logging_timestamp}: Polygon {polygon_id} successfully processed')

	return exports, exceptions

def export_try_except_loop(params, minutes_to_wait, exports, exceptions, attempts, debug=False):
	try:
		export = export_single_feature(**params)
		exports.append(export)
	except Exception as e:
		if debug:
			attempts += 1
			logging_timestamp = "_".join(time.ctime().split(" ")[1:])
			logging.info(f'{logging_timestamp}: Timeout #{attempts}; retrying in {minutes_to_wait} minutes')
		print(f'{e}; please wait for {minutes_to_wait} minutes', end='\r', flush=True)
		exceptions.append(e)
		# wait 30 minutes
		time.sleep(60 * minutes_to_wait)

		export_try_except_loop(params, minutes_to_wait, exports, exceptions, attempts)

def export_single_feature(offset_dict, roi=None, sensor=None, date_range=None, export_params=None, sort_by='CLOUDY_PIXEL_PERCENTAGE', polygon_id=None, area_limit=1000, skip_test=True, tile=None):
	modifiers = []
	if sensor['name'].lower() == "copernicus/s2_sr":
		#print('Inject B10')
		modifiers.append(inject_B10)
	if sensor['type'].lower() == "opt":
		#print(sensor['type'])
		modifiers += [sentinel2CloudScore, calcCloudCoverage, sentinel2ProjectShadows, computeQualityScore]
		#print(modifiers)
                    
	#print(f'Tile is {tile}')

	roi_ee = ee.Geometry.Polygon(roi)

	imgC = makeImageCollection(sensor, roi_ee, date_range['start_date'], date_range['end_date'], modifiers=modifiers, tile=tile)

	#print(f'Size of polygon {polygon_id}: {imgC.size().getInfo()}')
	# print(imgC.size().getInfo())
	# return None
	## sort was not in the original version
	#image_collection = image_collection.sort(sort_by)
	## below line was in the original verson;
	## changing to the JS version
	## img = image_collection.mosaic().clip(roi_ee)

	if skip_test is True:
		cloudFree = mergeCollection(imgC, polygon_id=polygon_id, date_range=date_range, test_coll=False)
	elif (date_range['day_offset'] == 'two years'): #or (date_range['area'] >= area_limit)
		# If we're pulling from two years, we'll end the dynamic date range loop and just have
		# this collection be the final one.
		cloudFree = mergeCollection(imgC, polygon_id=polygon_id, date_range=date_range, test_coll=False)
	else:
		cloudFree = mergeCollection(imgC, polygon_id=polygon_id, date_range=date_range, test_coll=True)
		# col1,col2 = mergeCollection(imgC, polygon_id=polygon_id, date_range=date_range, test_coll=True)
		# return col1,col2

	if cloudFree is None:
		original_date = date_range['original_date']
		area = date_range['area']
		day_offset = date_range['day_offset']

		# offset_dict = {
		# 	45: 90,
		# 	90: 180,
		# 	180: 'two years'
		# }

		# offset_dict = {
		# 	30: 'two years'
		# }

		new_offset = offset_dict[day_offset]

		if new_offset == 'two years':
			start_date = '2019-01-01'
			end_date = '2020-12-31'
		else:
			start = original_date + DateOffset(days=-day_offset)
			end = original_date + DateOffset(days=day_offset)
			start_date = str(start)[:10]
			end_date = str(end)[:10]

		# if tile is None:
		# 	print(f'Polygon {polygon_id} is increasing offset to {new_offset}')
		# else:
		# 	print(f'Tile {tile} is increasing offset to {new_offset}')

		new_date_range = {
			'start_date': start_date,
			'end_date': end_date,
			'original_date': original_date,
			'day_offset': new_offset,
			'area': area
		}

		return export_single_feature(
					offset_dict=offset_dict,
					roi=roi,
					sensor=sensor,
					date_range=new_date_range,
					export_params=export_params,
					sort_by=sort_by,
					polygon_id=polygon_id,
					tile=tile,
					skip_test=False
				)

	else:
		# if tile is None:
		# 	print(f'Polygon {polygon_id} successfully merged with offset {date_range["day_offset"]}')
		# else:
		# 	print(f'Tile {tile} successfully merged with offset {date_range["day_offset"]}')

		if tile is None:
			## when doing a feature collection
			# print('clipping to ROI in export_single_feature')
			cloudFree = cloudFree.clip(roi_ee).reproject('EPSG:4326', None, 10)
		else:
			## when doing tile by tile
			# print('not clipping in export_single_feature')
			cloudFree = cloudFree.reproject('EPSG:4326', None, 10)
		## Do we need to mosaic it now???
		# print('cloudFree info:', cloudFree.getInfo())
		# print('Mosaic type:', type(img))

		## make NDVI band
		ndvi = cloudFree.normalizedDifference(['B8', 'B4']).rename('NDVI')
		cloudFree = cloudFree.addBands(ndvi)
		cloudFree = cloudFree.float()

		new_params = export_params.copy()
		new_params['img'] = cloudFree
		new_params['roi'] = roi
		new_params['sensor_name'] = sensor['name'].lower()
		new_params['bands'] = sensor['bands']
		
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

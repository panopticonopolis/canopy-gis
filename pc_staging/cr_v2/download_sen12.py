import ee
import yaml
import time
import os
import json
import time
import pandas as pd
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


class Pipeline:
	def __init__(self, config_file, polygons=None, dynamic_date_range=False,
				 date_range_list=None, offset_array=None, debug=False):
		self.load_config(config_file)
		self.polygons = polygons
		self.dynamic_date_range = dynamic_date_range
		self.date_range_list = date_range_list
		self.debug = debug

		if self.polygons:
			if self.date_range_list is None:
				raise ValueError('If you input polygons, you must also input a date_range_list')
		if self.date_range_list:
			self._check_date_range_list()
		if self.dynamic_date_range:
			if offset_array:
				self.offset_dict = self._create_offset_dict(offset_array)
			else:
				if self.polygons:
					raise ValueError('With dynamic date range, if you input polygons you must also input an offset_array')

		self._initialize_ee()


	def _initialize_ee(self):
		try:
			ee.Initialize()
		except:
			ee.Authenticate()
			ee.Initialize() 


	def _check_date_range_list(self):
		t = type(self.date_range_list)
		if t is not list:
			raise ValueError(f'date_range_list is a {t}, but it must be a list')

		polygon_type = type(self.polygons)
		if self.polygons is None:
			raise ValueError('If you input a date_range_list, you must also input polygons')
		elif type(polygon_type) is ee.featurecollection.FeatureCollection:
			feature_list = self.polygons.toList(self.polygons.size())
			n_polygons = feature_list.size().getInfo()
		else:
			raise ValueError(f'Polygons are a {polygon_type}, but they must be an ee.FeatureCollection')

		if len(self.date_range_list) != n_polygons:
			raise ValueError('date_range_list and polygons must have the same length')

		offset_value = None
		for d in self.date_range_list:
			if type(d) is not dict:
				raise ValueError(f'date_range_list contains a {type(d)}, but it must solely contain dictionaries')

			if self.dynamic_date_range:
				required_keys = ['start_date', 'end_date', 'original_date', 'day_offset']
			else:
				required_keys = ['start_date', 'end_date']
			if len(d.keys()) != len(required_keys):
				if self.dynamic_date_range:
					raise ValueError(f'With dynamic date range, all dicts must have {len(required_keys)} keys, but at least one has {len(d.keys())} keys')
				else:
					raise ValueError(f'Without dynamic date range, all dicts must have {len(required_keys)} keys, but at least one has {len(d.keys())} keys')

			for key in d.keys():
				if key not in required_keys:
					raise KeyError(f'At least one dict has an inappropriate key: {key}')
				elif (self.dynamic_date_range is True) and (key == 'day_offset'):
					if offset_value is None:
						offset_value = d[key]
					else:
						if d[key] != offset_value:
							raise ValueError('Every date range in the date_range_list must have the same day_offset')


	def _create_offset_dict(self, offset_array):
		if type(offset_array) is not list:
			raise ValueError(f'offset_array is a {type(offset_array)}, but it must be a list')

		offset_array.sort()
		off_arr = offset_array + ['from 2019']
		d = {}
		for i, value in enumerate(off_arr):
			if value != 'from 2019':
				d[value] = off_arr[i+1]

		return d


	def _create_filename_parts(self):
		if isinstance(self.source['name'], str):
			self.source['name'] = [self.source['name']]

		if 'prefix' in self.sensor:
			if isinstance(self.sensor['prefix'], str):
				self.sensor['prefix'] = [self.sensor['prefix']]
			filename_parts = self.sensor['prefix'] + self.source['name']
		else:
			filename_parts = self.source['name']

		return filename_parts


	def _get_feature_area(self, feature):
		stateArea = feature.geometry().area()
    	stateAreaSqKm = ee.Number(stateArea).divide(1e6).round()

    	return stateAreaSqKm.getInfo()


	def load_config(self, config_file):
		stream = open(config_file, 'r') 
		config_dict = yaml.load(stream)
		self.source = config_dict['data_list'][0]
		self.sensor = config_dict['sensors'][0]
		self.export_folder = config_dict['bucket']
		return config_dict


	def import_aois(self, csv_loc, Full_Congo_Pull=False, start_date=None,
					end_date=None, days_duration=90, poly_start=0, poly_limit=None):
		features = []
		polygons = []
		day_offset = days_duration / 2
		start_end_list = []
		
		if Full_Congo_Pull:
			with open(csv_loc,"r",encoding='utf-8') as jsonfile:
				data = json.load(jsonfile)
				for feature_id, geometry in enumerate(data["features"], 1):
					polygon = geometry["geometry"]["coordinates"][0][0]
					poly_obj = ee.Geometry.Polygon(polygon)
					feature = ee.Feature(poly_obj, {"name": feature_id})
					features.append(feature)
					
				original_date = pd.to_datetime('01-01-2020')
				start = (original_date + DateOffset(days=-30))
				end = (original_date + DateOffset(days=30))
				start_date = str(start)[:10]
				end_date = str(end)[:10]
				date_dict = {
					'start_date': start_date,
					'end_date': end_date,
					'original_date': original_date,
					'day_offset': 30,
				}
				
				start_end_list = [date_dict] * len(features)
				fc = ee.FeatureCollection(features)

				offset_dict = {
					30: 'two years'
				}
				
				self.polygons = fc
				self.date_range_list = start_end_list
				self.offset_dict = offset_dict
				return fc, start_end_list, offset_dict
				
		else:
			feature_id = poly_start
			
			if poly_limit:
				df_labels = pd.read_csv(csv_loc, skiprows=range(1, poly_start+1), nrows=poly_limit)
			else:
				df_labels = pd.read_csv(csv_loc, skiprows=range(1, poly_start+1))

			df_labels = df_labels[["center-lat","center-long","polygon","Labels combined","tile date","area (km2)"]]
			df_labels["tile date"] = pd.to_datetime(df_labels["tile date"])
			start = (df_labels["tile date"] + DateOffset(days=-day_offset))
			end = (df_labels["tile date"] + DateOffset(days=day_offset))
			for i in range(len(start)):
				start_date = str(start[i])[:10]
				end_date = str(end[i])[:10]
				original_date = df_labels.loc[i, 'tile date']
				date_dict = {
					'start_date': start_date,
					'end_date': end_date,
					'original_date': original_date,
					'day_offset': day_offset,
				}
				start_end_list.append(date_dict)

			for polygon in df_labels["polygon"]:
				polygons.append(json.loads(polygon)["coordinates"])
				
			for poly in polygons:
				# create an roi. first item in Misha's label list
				feature_id += 1 
				# create geometry object, create feature object, append to features list for feature collection creation 
				polys = ee.Geometry.Polygon(poly)
				feature = ee.Feature(polys,{"name":feature_id})
				features.append(feature)

			fc = ee.FeatureCollection(features)

			offset_dict = {
				45: 90,
				90: 180,
				180: 'two years'
			}
				
			self.polygons = fc
			self.date_range_list = start_end_list
			self.offset_dict = offset_dict
			return fc, start_end_list, offset_dict


	def process_datasource_custom_daterange(
		self, loop_start=0, limit=None, minutes_to_wait=60
	):
		# source = self.source
		# sensor = self.sensor
		# export_folder = self.export_folder
		# polygons = self.polygons
		# date_range_list = self.date_range_list
		# debug = self.debug

		feature_list = self.polygons.sort(self.source['sort_by']).toList(self.polygons.size())
		n_polygons = feature_list.size().getInfo()
		print(f"{n_polygons} features have been loaded")

		#task_list = []

		exports = []
		exceptions = []

		filename_parts = self._create_filename_parts()

		if not limit:
			limit = n_polygons

		### ERROR? ###
		## Originally this was range(1, n_features), but we're pretty sure
		## that should be 0 so we changed it.
		for i in range(loop_start, limit):
			polygon_id = i + 1

			print(f'Processing polygon {polygon_id} of {limit}', end='\r', flush=True)

			feature = ee.Feature( feature_list.get(i) )
			if self._get_feature_area(feature) > 1000:
				print(f'Polygon {polygon_id} has area greater than 1000; skipping')
			else:
			roi = feature.geometry()
			roi = roi.coordinates().getInfo()[0]
			tile = None

			time_stamp = "_".join(time.ctime().split(" ")[1:])
			filename = "_".join([str(polygon_id)] + self.source['name'] + [time_stamp])
			dest_path = "/".join(filename_parts + [filename])

			self.export_params = {
				'bucket': self.export_folder,
				'resolution': self.source['resolution'],
				'filename': filename,
				'dest_path': dest_path
			}


			date_range = date_range_list[i]

			params = {
				'roi': roi,
				#'sensor': sensor, ## object attribute
				'date_range': date_range,
				#'export_params': export_params, ## object attribute
				#'sort_by': pre_mosaic_sort, ## unnecessary
				'polygon_id': polygon_id,
				'area_limit': area_limit,
				'skip_test': False,
				'tile': tile,
				'offset_dict': offset_dict
			}

			export_try_except_loop(params, minutes_to_wait, exports, exceptions, 0, debug)

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

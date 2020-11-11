// DOWNLOAD SETTINGS
var gcs_bucket = 'test_javascript'; // Google Cloud bucket name
var debug = 1; // Set the debug level for the script, use 0 when in production

// ALGORITHM SETTINGS
var cloudThresh = 0.2;//Ranges from 0-1.Lower value will mask more pixels out. Generally 0.1-0.3 works well with 0.2 being used most commonly 
var cloudHeights = ee.List.sequence(200,10000,250);//Height of clouds to use to project cloud shadows
var irSumThresh = 0.3;//Sum of IR bands to include as shadows within TDOM and the shadow shift method (lower number masks out less)
var ndviThresh = -0.1;
var dilatePixels = 2; //Pixels to dilate around clouds
var contractPixels = 1;//Pixels to reduce cloud mask and dark shadows by to reduce inclusion of single-pixel comission errors
var erodePixels = 1.5;
var dilationPixels = 3;
var cloudFreeKeepThresh = 5;
var cloudMosaicThresh = 50;

// calcCloudStats: Calculates a mask for clouds in the image.
//        input: im - Image from image collection with a valid mask layer
//        output: original image with added stats. 
//                - CLOUDY_PERCENTAGE: The percentage of the image area affected by clouds
//                - ROI_COVERAGE_PERCENT: The percentage of the ROI region this particular image covers
//                - CLOUDY_PERCENTAGE_ROI: The percentage of the original ROI which is affected by the clouds in this image
//                - cloudScore: A per pixel score of cloudiness
function calcCloudStats(img){
    var imgPoly = ee.Algorithms.GeometryConstructors.Polygon( 
              ee.Geometry( img.get('system:footprint') ).coordinates() 
              )
    
    var roi = ee.Geometry(img.get('ROI'))

    var intersection = roi.intersection(imgPoly, ee.ErrorMargin(0.5))
    var cloudMask = img.select(['cloudScore']).gt(cloudThresh).clip(roi).rename('cloudMask')

    var cloudAreaImg = cloudMask.multiply(ee.Image.pixelArea())

    var stats = cloudAreaImg.reduceRegion({
      reducer: ee.Reducer.sum(),
      geometry: roi,
      scale: 10,
      maxPixels: 1e12
    })
    
    var cloudPercent = ee.Number(stats.get('cloudMask')).divide(imgPoly.area()).multiply(100)
    var coveragePercent = ee.Number(intersection.area()).divide(roi.area()).multiply(100)
    var cloudPercentROI = ee.Number(stats.get('cloudMask')).divide(roi.area()).multiply(100)

    img = img.set('CLOUDY_PERCENTAGE', cloudPercent)
    img = img.set('ROI_COVERAGE_PERCENT', coveragePercent)
    img = img.set('CLOUDY_PERCENTAGE_ROI', cloudPercentROI)

    return img
}

function rescale(img, exp, thresholds) {
    return img.expression(exp, {img: img})
              .subtract(thresholds[0])
              .divide(thresholds[1] - thresholds[0])
}

function computeQualityScore(img) {
    var score = img.select(['cloudScore']).max(img.select(['shadowScore']))

    score = score.reproject('EPSG:4326', null, 20).reduceNeighborhood(
      {
        reducer: ee.Reducer.mean(),
        kernel: ee.Kernel.square(5)
      })

    score = score.multiply(-1)
    
    return img.addBands(score.rename('cloudShadowScore'))
}

function computeS2CloudScore(img) {
    var toa = img.select(['B1','B2','B3','B4','B5','B6','B7','B8','B8A', 'B9','B10', 'B11','B12'])
              .divide(10000)

    toa = toa.addBands(img.select(['QA60']))

    // ['QA60', 'B1','B2',    'B3',    'B4',   'B5','B6','B7', 'B8','  B8A', 'B9',          'B10', 'B11','B12']
    // ['QA60','cb', 'blue', 'green', 'red', 're1','re2','re3','nir', 'nir2', 'waterVapor', 'cirrus','swir1', 'swir2']);

    // Compute several indicators of cloudyness and take the minimum of them.
    var score = ee.Image(1);

    // Clouds are reasonably bright in the blue and cirrus bands.
    score = score.min(rescale(toa, 'img.B2', [0.1, 0.5]));
    score = score.min(rescale(toa, 'img.B1', [0.1, 0.3]));
    score = score.min(rescale(toa, 'img.B1 + img.B10', [0.15, 0.2]));

    // Clouds are reasonably bright in all visible bands.
    score = score.min(rescale(toa, 'img.B4 + img.B3 + img.B2', [0.2, 0.8]));

    //Clouds are moist
    var ndmi = img.normalizedDifference(['B8','B11']);
    score=score.min(rescale(ndmi, 'img', [-0.1, 0.1]));

    // However, clouds are not snow.
    var ndsi = img.normalizedDifference(['B3', 'B11']);
    score=score.min(rescale(ndsi, 'img', [0.8, 0.6]));

    // Clip the lower end of the score
    score = score.max(ee.Image(0.001))
    
    // Remove small regions and clip the upper bound
    var dilated = dilatedErossion(score).min(ee.Image(1.0))
    
    // score = score.multiply(dilated)
    score = score.reduceNeighborhood(
      {
        reducer: ee.Reducer.mean(),
        kernel: ee.Kernel.square(5)
      })

    return img.addBands(score.rename('cloudScore'));
}

/***
 * Implementation of Basic cloud shadow shift
 * 
 * Author: Gennadii Donchyts
 * License: Apache 2.0
 */
function projectShadows(image){
  var meanAzimuth = image.get('MEAN_SOLAR_AZIMUTH_ANGLE');
  var meanZenith = image.get('MEAN_SOLAR_ZENITH_ANGLE');
  
  var cloudMask = image.select(['cloudScore']).gt(cloudThresh)

  //Find dark pixels
  var darkPixelsImg = image.select(['B8','B11','B12'])
                    .divide(10000)
                    .reduce(ee.Reducer.sum())

  var ndvi = image.normalizedDifference(['B8','B4'])
  var waterMask = ndvi.lt(ndviThresh)

  var darkPixels = darkPixelsImg.lt(irSumThresh)

  // Get the mask of pixels which might be shadows excluding water
  var darkPixelMask = darkPixels.and(waterMask.not())
  darkPixelMask = darkPixelMask.and(cloudMask.not())
 
  //Find where cloud shadows should be based on solar geometry
  //Convert to radians
  var azR = ee.Number(meanAzimuth).add(180).multiply(Math.PI).divide(180.0);
  var zenR = ee.Number(meanZenith).multiply(Math.PI).divide(180.0);
  
  //Find the shadows
  var shadows = cloudHeights.map(function(cloudHeight){
    cloudHeight = ee.Number(cloudHeight);
 
    var shadowCastedDistance = zenR.tan().multiply(cloudHeight);//Distance shadow is cast
    var x = azR.sin().multiply(shadowCastedDistance).multiply(-1)//.divide(nominalScale);//X distance of shadow
    var y = azR.cos().multiply(shadowCastedDistance).multiply(-1);//Y distance of shadow
    return image.select(['cloudScore']).displace(ee.Image.constant(x).addBands(ee.Image.constant(y)));
  });
  
  var shadowMasks = ee.ImageCollection.fromImages(shadows)
  var shadowMask = shadowMasks.mean()

  // //Create shadow mask
  shadowMask = dilatedErossion(shadowMask.multiply(darkPixelMask));
  
  var shadowScore = shadowMask.reduceNeighborhood(
      {
        reducer: ee.Reducer.max(),
        kernel: ee.Kernel.square(1)
      })

  image = image.addBands(shadowScore.rename(['shadowScore']))

  return image;
}

function dilatedErossion(score) {
  // Perform opening on the cloud scores
  score = score
            .reproject('EPSG:4326', null, 20)
            .focal_min({radius: erodePixels, kernelType: 'circle', iterations:3})
            .focal_max({radius: dilationPixels, kernelType: 'circle', iterations:3})
            .reproject('EPSG:4326', null, 20)
            
  return(score)
}

// mergeCollection: Generates a single non-cloudy Sentinel 2 image from a processed ImageCollection
//        input: imgC - Image collection including "cloudScore" band for each image
//              threshBest - A threshold percentage to select the best image. This image is used directly as "cloudFree" if one exists.
//              threshMed - A max threshold to select all image which are to be merged to create the cloud free image
//        output: A single cloud free mosaic for the region of interest
function mergeCollection(imgC) {
    // Select the best images, which are below the cloud free threshold, sort them in reverse order (worst on top) for mosaicing
    var best = imgC.filterMetadata('CLOUDY_PERCENTAGE', 'less_than', cloudFreeKeepThresh).sort('CLOUDY_PERCENTAGE',false)
    var filtered = imgC.qualityMosaic('cloudShadowScore')

    // Add the quality mosaic to fill in any missing areas of the ROI which aren't covered by good images
    var newC = ee.ImageCollection.fromImages( [filtered, best.mosaic()] )

    return ee.Image(newC.mosaic())
}

function uniqueValues(collection,field){
    var values = ee.Dictionary( 
          collection.reduceColumns(ee.Reducer.frequencyHistogram(), [field])
          .get('histogram')
        ).keys()

    return values;
}

function dailyMosaics(imgs){
    //Simplify date to exclude time of day
    imgs = imgs.map(function(img) {
      var d = ee.Date(img.get('system:time_start'));
      var day = d.get('day');
      var m = d.get('month');
      var y = d.get('year');
      var simpleDate = ee.Date.fromYMD(y,m,day);
      return img.set('simpleTime',simpleDate.millis());
    });

    //Find the unique days
    var days = uniqueValues(imgs,'simpleTime');

    imgs = days.map(function(d) {
        d = ee.Number.parse(d);
        d = ee.Date(d);
        var t = imgs.filterDate(d, d.advance(1,'day'));
        var f = ee.Image(t.first());
        t = t.mosaic();
        t = t.set('system:time_start', d.millis());
        t = t.copyProperties(f);
        return t;
      });

      imgs = ee.ImageCollection.fromImages(imgs);

      return imgs;
}

// exportCloudFreeSen2: Exports a cloud free image for a specific date range to GCS in 3 GeoTIFFs (1 per band resolution)
//        input: season - A text name for the season. Used for naming the exported files.
//               dates - Array of the date range for the mosaicing [start, end]
//               roi - Region of interest to clip, for exporting
//               roiID - A unique identifier for the ROI, used for naming files
//               debug - A debugging level for displaying data on the map. (0-off, 2-everything)
//        output: None
function exportCloudFreeSen2(season, dates, roiID, roi, debug) {
    debug = typeof debug !== 'undefined' ? debug : 0

    var bands = {
                "10": ee.List([ 'B2', 'B3', 'B4',  'B8' ]),
                "20": ee.List([ 'B5', 'B6', 'B7', 'B8A', 'B11', 'B12' ]),
                "60": ee.List([ 'B1', 'B9', 'B10' ])
                }

    var startDate = ee.DateRange(dates).start()
    var endDate = ee.DateRange(dates).end()
    
    //Filter images of this period
    var imgC = ee.ImageCollection("COPERNICUS/S2")
              .filterDate(startDate, endDate)
              .filterBounds(roi)

    imgC = imgC.map( function(x) { return(x.clip(roi)) } ) // Clip the image to the ROI
              .map( function(x) { return(x.set('ROI', roi)) } ) // Store the ROI with the image as we cannot pass it around safely otherwise
              .map(computeS2CloudScore)
              .map(calcCloudStats)
              .map(projectShadows)
              .map(computeQualityScore)
              .sort('CLOUDY_PERCENTAGE')

    var cloudFree = mergeCollection(imgC)
    cloudFree = cloudFree.reproject('EPSG:4326', null, 10)

    if (debug > 0) {
      if (debug > 3) {
        for (var i = 0; i < parseInt(imgC.size().getInfo()); i++) {
           Map.addLayer(ee.Image(imgC.toList(imgC.size()).get(i) ), {bands: ['B4', 'B3', 'B2'], max: 4000},  i, false);
        }
      }
      if (debug > 2) {
        Map.addLayer(ee.Image(imgC.toList(imgC.size()).get(0) ), {bands: ['cloudScore'], max: 1},  'cloudScore0', false);
        Map.addLayer(ee.Image(imgC.toList(imgC.size()).get(1) ), {bands: ['cloudScore'], max: 1},  'cloudScore1', false);
        Map.addLayer(ee.Image(imgC.toList(imgC.size()).get(0) ), {bands: ['shadowScore'], max: 1},  'shadowScore0', false);
        Map.addLayer(ee.Image(imgC.toList(imgC.size()).get(1) ), {bands: ['shadowScore'], max: 1},  'shadowScore1', false);
        Map.addLayer(ee.Image(imgC.toList(imgC.size()).get(0) ), {bands: ['cloudShadowScore'], max: 1},  'cloudShadowScore0', false);
        Map.addLayer(ee.Image(imgC.toList(imgC.size()).get(1) ), {bands: ['cloudShadowScore'], max: 1},  'cloudShadowScore1', false);
      }
      if (debug > 1) {
        Map.addLayer(ee.Image(imgC.toList(imgC.size()).get(0) ), {bands: ['B4', 'B3', 'B2'], max: 4000},  '0');
        Map.addLayer(ee.Image(imgC.toList(imgC.size()).get(1) ), {bands: ['B4', 'B3', 'B2'], max: 4000},  '1');
      }
        print(imgC)
        Map.addLayer(cloudFree, {bands: ['B1', 'B9', 'B10'], max: 4000},  'cloudFree_'+season)
        Map.centerObject(roi, 11)
    }

    // Export the various resolutions to the correct image folders
    // Assumes the GCS bucket is called sentinel2
    // Exports to folder structure "city/season/city_season_resolution.tiff"

      var filename = ''+roiID+'_'+season;

      Export.image.toCloudStorage({
        image: cloudFree.select(['B1','B2','B3','B4','B5','B6','B7','B8','B8A', 'B9','B10', 'B11','B12']),
        description: filename,
        scale: 10,
        region: roi,
        fileNamePrefix: ''+roiID+'/'+season+'/'+filename,
        bucket: gcs_bucket,
        maxPixels: 1e13
      })

    return(cloudFree)
}

// downloadSeasonsSen2: Exports all GeoTIFFs for all seasons for specific ROI
//        input: roi - Region of interest to clip, for exporting
//               roiID - A unique identifier for the ROI, used for naming files
//        output: None
function downloadSeasonsSen2(roi, roiID) {
   // DateRange is start inclusive and end exclusive
    var seasons = {
                  "spring": ee.DateRange('2017-03-01', '2017-06-01'),
                  "summer": ee.DateRange('2017-06-01', '2017-09-01'),
                  "autumn": ee.DateRange('2017-09-01', '2017-12-01'),
                  "winter": ee.DateRange('2017-12-01', '2018-03-01'),
                  }

    for (var season in seasons) {
        var dates = seasons[season]
        exportCloudFreeSen2(season, dates, roiID, roi, debug)
    }
    
    return roiID
}

var geometry = ee.Geometry.Polygon(
        [[[11.561171012727641, 48.16522454214382],
          [11.561171012727641, 47.964236011094975],
          [11.848188834993266, 47.964236011094975],
          [11.848188834993266, 48.16522454214382]]]);
        
Map.addLayer(geometry)
Map.centerObject(geometry, 10)

var cloudFree = downloadSeasonsSen2(geometry, "MUNICH")  
print(cloudFree)
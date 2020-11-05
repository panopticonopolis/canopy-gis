import ee

from helpers import rescale, dilatedErossion

def sentinel2CloudScore(img):
    """
    Calculates the Cloud Score of the input Earth Engine image,
    and returns that image with the Cloud Score as a band called "cloudScore."
    """
    # Create a new image object, "toa." Select bands B1-B12,
    # then divide their values by 10,000.
    toa = img.select(['B1','B2','B3','B4','B5','B6','B7','B8','B8A', 'B9','B10', 'B11','B12']) \
              .divide(10000)

    # Add band QA60. Don't change the values
    toa = toa.addBands(img.select(['QA60']))

    # ['QA60', 'B1','B2',   'B3',    'B4',  'B5', 'B6', 'B7', 'B8',''B8A', 'B9',         'B10',   'B11',   'B12']
    # ['QA60', 'cb','blue', 'green', 'red', 're1','re2','re3','nir','nir2','waterVapor', 'cirrus','swir1', 'swir2'])

    # Compute several indicators of cloudyness and take the minimum of them.
    score = ee.Image(1)

    # Clouds are reasonably bright in the blue and cirrus bands.
    score = score.min(rescale(toa, 'img.B2', [0.1, 0.5]))
    score = score.min(rescale(toa, 'img.B1', [0.1, 0.3]))
    score = score.min(rescale(toa, 'img.B1 + img.B10', [0.15, 0.2]))

    # Clouds are reasonably bright in all visible bands.
    score = score.min(rescale(toa, 'img.B4 + img.B3 + img.B2', [0.2, 0.8]))

    #Clouds are moist
    ndmi = img.normalizedDifference(['B8','B11'])
    score=score.min(rescale(ndmi, 'img', [-0.1, 0.1]))

    # However, clouds are not snow.
    ndsi = img.normalizedDifference(['B3', 'B11'])
    score=score.min(rescale(ndsi, 'img', [0.8, 0.6]))

    ### The above is the same as the Hancher/Hewig/Housman code;
    ### the below appears to be new
    # Clip the lower end of the score
    score = score.max(ee.Image(0.001))

    # Remove small regions and clip the upper bound
    ### Appears to get rid of cloud pixels that are all by themselves
    dilated = dilatedErossion(score).min(ee.Image(1.0))

    ### SHOULD THIS BE "dilated"? ###
    score = score.reduceNeighborhood(
        reducer=ee.Reducer.mean(),
        kernel=ee.Kernel.square(5)
      )

    return img.addBands(score.rename('cloudScore'))

# GEE_DataDownloader
A tool to manage downloading and preprocessing of data from Google Earth Engine.

Note: Due to Google scrapping FusionTables this tool no longer works and will require some minor modifications to get up and running again. Fee free to submit a PR.

The tool handles:
- Querying GEE according to the requirements in the config file
- Preprocessing/filtering the data as needed (Cloud masking, Cloud free mosaic etc)
- Exporting the data to GCS bucket for mass download
- Handling all export tasks and retrying on failure

It realies on our cloud masking and mosaicing methods which are contained in the GEE_Utils library https://github.com/SiPEO/GEE_Utils

This code is free to use for academic and non-commercial purposes (in order to stay in line with the GEE terms of use). If you make use of our code please cite the following works respectively

GEE_DataDownloader:

> Schmitt, M., Hughes, L. H., Qiu, C., & Zhu, X. X. (2019). SEN12MS--A Curated Dataset of Georeferenced Multi-Spectral Sentinel-1/2 Imagery for Deep Learning and Data Fusion. arXiv preprint arXiv:1906.07789.

GEE_Utils (Cloud and Shadow Masking):
> Schmitt, M., Hughes, L. H., Qiu, C., and Zhu, X. X.: AGGREGATING CLOUD-FREE SENTINEL-2 IMAGES WITH GOOGLE EARTH ENGINE, ISPRS Ann. Photogramm. Remote Sens. Spatial Inf. Sci., IV-2/W7, 145–152, https://doi.org/10.5194/isprs-annals-IV-2-W7-145-2019, 2019.


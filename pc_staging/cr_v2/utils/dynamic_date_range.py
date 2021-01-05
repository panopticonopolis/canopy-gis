import ee
import pandas as pd


def coll_filter_conditional(coll, filter_by='NODATA_PIXEL_PERCENTAGE',
                            filter_type='less_than', filter_thresh=10,
                            cond_type='gte', cond_thresh=30,
                            print_size=False, print_mod=None):
    if filter_by is not None:
        coll = coll.filterMetadata(filter_by, filter_type, filter_thresh)
        
    size = coll.size()
    
    if print_size:
        print(f'Size {print_mod}:', size.getInfo())
    
    flag = getattr(size, cond_type)(cond_thresh)
    
    return ee.Algorithms.If(flag, True, False).getInfo()


def collection_greater_than(coll, threshold, print_mod=None):
    return coll_filter_conditional(coll, filter_by=None, cond_thresh=threshold, print_mod=print_mod)


def collection_quality_test_no_filter(coll, best, coll_thresh=30, best_thresh=1):
    best_is_good = collection_greater_than(best, best_thresh)
    if best_is_good:
        return True
    
    return collection_greater_than(coll, coll_thresh)


def collection_quality_test_filter(coll, best, coll_min=30, best_min=1,
                                   filter_by='NODATA_PIXEL_PERCENTAGE',
                                   filter_type='less_than', filter_thresh=10):

    # Test to see if there's at least one good image with low nodata pixels
    best_is_good = coll_filter_conditional(best, filter_by=filter_by, filter_type=filter_type,
                                           filter_thresh=filter_thresh, cond_thresh=best_min,
                                           print_mod='of best filtered')
    
    # If there is, the collection is fine!
    if best_is_good:
        return True
    
    # If there isn't a sufficient good image, test the size of the collection as a whole
    coll_is_good = collection_greater_than(coll, coll_min, print_mod='of entire coll')

    return coll_is_good

def image_collection_secondary_sort(col,primary_sort=None,secondary_sort=None):

    new_list_of_images = []
    primary_list = col.aggregate_array(primary_sort)
    secondary_list = col.aggregate_array(secondary_sort)
    # image_id_list = col.aggregate_array('system:index')
    
    
    # sort_dic = \
    # {primary_sort:primary_list,
    #  secondary_sort:secondary_list,
    #  "id":image_id_list}

    sort_dic = \
    {primary_sort:primary_list,
     secondary_sort:secondary_list}
    

    new_sort_dic = {}
    #print('secondary sort -- getting infos')
    for key in sort_dic:
        new_sort_dic[key] = sort_dic[key].getInfo()
        #print(f'got info for {key}')
        
    df = pd.DataFrame(new_sort_dic)
    
    df = df.sort_values(by=[primary_sort,secondary_sort],ascending=False)
    
    df = df.reset_index()
    
    df = df.reset_index()
    
    df.rename(columns = {'index':'current_position', 'level_0':'new_position'}, inplace = True) 
    
    
    list_of_images = col.toList(col.size())
    

    for row in df.iterrows():
        origin = row[1][1]

        img_dest = ee.Image(list_of_images.get(origin))

        new_list_of_images.append(img_dest)

        
    return ee.ImageCollection(new_list_of_images)
    

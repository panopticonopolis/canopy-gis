import ee


def coll_filter_conditional(coll, filter_by='NODATA_PIXEL_PERCENTAGE',
                            filter_type='less_than', filter_thresh=10,
                            cond_type='gte', cond_thresh=30,
                            print_mod=None):
    if filter_by is not None:
        coll = coll.filterMetadata(filter_by, filter_type, filter_thresh)
        
    size = coll.size()
    
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

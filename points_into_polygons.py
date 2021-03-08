import geopandas as gpd
import logging
import numpy as np
import os
import pandas as pd
import re
import shapely
import sys
from bisect import bisect
from collections import OrderedDict
from operator import add, index, itemgetter
from shapely.geometry import Point, Polygon, MultiPolygon

sys.path.insert(1, os.path.join(sys.path[0], ".."))
import helpers

'''
This script is a proof of conept building on the work of Jessie Stewart for the NRN. This script attempts to take
building foorprints and match the best address point available to them in order to apply pertinent address fields
to the building fooprints.

'''
# ------------------------------------------------------------------------------------------------------------
# Functions

def explode(ingdf):
    # not one of Jesse's. To solve multipolygon issue
    indf = ingdf
    outdf = gpd.GeoDataFrame(columns=indf.columns)
    for idx, row in indf.iterrows():
        
        if type(row.geometry) == Polygon:
            outdf = outdf.append(row,ignore_index=True)
        if type(row.geometry) == MultiPolygon:
            multdf = gpd.GeoDataFrame(columns=indf.columns)
            recs = len(row.geometry)
            multdf = multdf.append([row]*recs,ignore_index=True)
            for geom in range(recs):
                multdf.loc[geom,'geometry'] = row.geometry[geom]
            outdf = outdf.append(multdf,ignore_index=True)
    return outdf

def groupby_to_list(df, group_field, list_field):
    """
    Helper function: faster alternative to pandas groupby.apply/agg(list).
    Groups records by one or more fields and compiles an output field into a list for each group.
    """
    
    if isinstance(group_field, list):
        for field in group_field:
            if df[field].dtype.name != "geometry":
                df[field] = df[field].astype("U")
        transpose = df.sort_values(group_field)[[*group_field, list_field]].values.T
        keys, vals = np.column_stack(transpose[:-1]), transpose[-1]
        keys_unique, keys_indexes = np.unique(keys.astype("U") if isinstance(keys, np.object) else keys, 
                                              axis=0, return_index=True)
    
    else:
        keys, vals = df.sort_values(group_field)[[group_field, list_field]].values.T
        keys_unique, keys_indexes = np.unique(keys, return_index=True)
    
    vals_arrays = np.split(vals, keys_indexes[1:])
    
    return pd.Series([list(vals_array) for vals_array in vals_arrays], index=keys_unique).copy(deep=True)

def as_int(val):
    "Step 2: Converts linkages to integer tuples, if possible"
    try:
        return int(val)
    except ValueError:
        return val

def get_nearest_linkage(pt, roadseg_indexes):
    """Returns the roadseg index associated with the nearest roadseg geometry to the given address point."""
    pt_geoseries = gpd.GeoSeries([pt])
    # Get roadseg geometries.
    roadseg_geometries = tuple(map(lambda index: roadseg["geometry"].loc[roadseg.index == index], roadseg_indexes))
    # Get roadseg distances from address point.
    roadseg_distances = pd.concat(tuple(map(lambda road: road.exterior.distance(pt), roadseg_geometries)))                                      
    # Get the roadseg index associated with the smallest distance.
    roadseg_index = roadseg_indexes[roadseg_indexes.index == int(roadseg_distances[roadseg_distances == roadseg_distances.min()].index[0])]
    
    return roadseg_index
  
# ---------------------------------------------------------------------------------------------------------------
# Inputs

output_path = r'H:\point_to_polygon_PoC'

# Layer inputs
project_gpkg = "H:/point_to_polygon_PoC/data/data.gpkg"
addresses_lyr_nme = "yk_Address_Points"
bf_lyr_nme = "yk_buildings_sj"
bf_polys = "yk_buildings_sj"

# ---------------------------------------------------------------------------------------------------------------
# Logic

# Load dataframes.
addresses = gpd.read_file(project_gpkg, layer= addresses_lyr_nme)
roadseg = gpd.read_file(project_gpkg, layer= bf_lyr_nme) # spatial join between the parcels and building footprints layers

print('Running Step 0. Clean Data')
# Clean data
# Remove rite of way from the address data and join count > 0
addresses = addresses[(addresses.CIVIC_ADDRESS != "RITE OF WAY")]

# Remove null street name rows
roadseg = roadseg[(roadseg.Join_Count > 0) & (roadseg.STREET_NAME.notnull()) & (roadseg.STREET_NAME != ' ')] 

roadseg = explode(roadseg)

print( "Running Step 1. Load dataframes and configure attributes")
# Define join fields.
join_roadseg = "STREET_NAME"
join_addresses = "STREET_NAME"

# Configure attributes - number and suffix.
addresses["suffix"] = addresses["CIVIC_ADDRESS"].map(lambda val: re.sub(pattern="\\d+", repl="", string=val, flags=re.I))
addresses["number"] = addresses["CIVIC_ADDRESS"].map(lambda val: re.sub(pattern="[^\\d]", repl="", string=val, flags=re.I)).map(int)

print("Running Step 2. Configure address to roadseg linkages")
# Link addresses and roadseg on join fields.
addresses["addresses_index"] = addresses.index
roadseg["roadseg_index"] = roadseg.index

merge = addresses.merge(roadseg[[join_roadseg, "roadseg_index"]], how="left", left_on=join_addresses, right_on=join_roadseg)
addresses["roadseg_index"] = groupby_to_list(merge, "addresses_index", "roadseg_index")

addresses.drop(columns=["addresses_index"], inplace=True)
roadseg.drop(columns=["roadseg_index"], inplace=True)

# Discard non-linked addresses.
addresses.drop(addresses[addresses["roadseg_index"].map(itemgetter(0)).isna()].index, axis=0, inplace=True)

# Convert linkages to integer tuples, if possible.

addresses["roadseg_index"] = addresses["roadseg_index"].map(lambda vals: tuple(set(map(as_int, vals))))

# Flag plural linkages.
flag_plural = addresses["roadseg_index"].map(len) > 1
addresses.to_csv(os.path.join(output_path, 'test.csv'))
# Reduce plural linkages to the road segment with the lowest (nearest) geometric distance.
addresses.loc[flag_plural, "roadseg_index"] = addresses[flag_plural][["geometry", "roadseg_index"]].apply(
    lambda row: get_nearest_linkage(*row), axis=1)

# Unpack first tuple element for singular linkages.
addresses.loc[~flag_plural, "roadseg_index"] = addresses[~flag_plural]["roadseg_index"].map(itemgetter(0))

# Compile linked roadseg geometry for each address.
addresses["roadseg_geometry"] = addresses.merge(
    roadseg["geometry"], how="left", left_on="roadseg_index", right_index=True)["geometry_y"]

print("Running Step 3. Final Merge to Polygons")

# Import the building polygons
building_polys = gpd.read_file(project_gpkg, layer= bf_polys)
out_gdf = building_polys.merge(addresses[['roadseg_index', 'number', 'suffix']], how="left", right_on="roadseg_index", left_index=True)
out_gdf.to_file(os.path.join(output_path, 'test_from_centroids.shp'))
print('DONE!')

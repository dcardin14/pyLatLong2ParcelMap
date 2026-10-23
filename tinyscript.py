import geopandas as gpd

gdf = gpd.read_file(
    r"counties\txdot_counties.geojson"
)

print(gdf.columns.tolist())
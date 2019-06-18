from datetime import date, datetime, timedelta
import datacube
try:
    from datacube_wms.wms_cfg_local import service_cfg, layer_cfg
except:
    from datacube_wms.wms_cfg import service_cfg, layer_cfg
from psycopg2.extras import Json
from itertools import zip_longest
from uuid import UUID


def accum_min(a, b):
    if a is None:
        return b
    elif b is None:
        return a
    else:
        return min(a, b)


def accum_max(a, b):
    if a is None:
        return b
    elif b is None:
        return a
    else:
        return max(a, b)


def determine_product_ranges(dc, product_name, time_offset, extractor):
    start = datetime.now()
    product = dc.index.products.get_by_name(product_name)
    print("Product: ", product_name)
    r = {
        "product_id": product.id,

        "lat": {
            "min": None,
            "max": None
        },
        "lon": {
            "min": None,
            "max": None
        },
    }
    sub_r = {}
    time_set = set()

    crsids = service_cfg["published_CRSs"]
    extents = {crsid: None for crsid in crsids}
    crses = {crsid: datacube.utils.geometry.CRS(crsid) for crsid in crsids}
    ds_count = 0
    for ds in dc.find_datasets(product=product_name):
        if extractor is not None:
            path = extractor(ds)
            if path not in sub_r:
                sub_r[path] = {
                    "product_id": product.id,
                    "sub_id": path,
                    "lat": {
                        "min": None,
                        "max": None,
                    },
                    "lon": {
                        "min": None,
                        "max": None,
                    },
                    "time_set": set(),
                    "extents": {crsid: None for crsid in crsids}
                }
            sub_r[path]["lat"]["min"] = accum_min(sub_r[path]["lat"]["min"], ds.metadata.lat.begin)
            sub_r[path]["lat"]["max"] = accum_max(sub_r[path]["lat"]["max"], ds.metadata.lat.end)
            sub_r[path]["lon"]["min"] = accum_min(sub_r[path]["lon"]["min"], ds.metadata.lon.begin)
            sub_r[path]["lon"]["max"] = accum_max(sub_r[path]["lon"]["max"], ds.metadata.lon.end)
        else:
            path = None

        r["lat"]["min"] = accum_min(r["lat"]["min"], ds.metadata.lat.begin)
        r["lat"]["max"] = accum_max(r["lat"]["max"], ds.metadata.lat.end)
        r["lon"]["min"] = accum_min(r["lon"]["min"], ds.metadata.lon.begin)
        r["lon"]["max"] = accum_max(r["lon"]["max"], ds.metadata.lon.end)

        dt = ds.center_time + timedelta(hours=time_offset)
        time_set.add(dt.date())
        if path is not None:
            sub_r[path]["time_set"].add(dt.date())

        for crsid in crsids:
            crs = crses[crsid]
            ext = ds.extent
            if ext.crs != crs:
                ext = ext.to_crs(crs)
            cvx_ext = ext.convex_hull
            if cvx_ext != ext:
                print ("INFO: Dataset", ds.id, "CRS", crsid, "extent is not convex.")
            if extents[crsid] is None:
                extents[crsid] = cvx_ext
            else:
                if not extents[crsid].is_valid:
                    print("WARNING: Extent Union for", ds.id, "CRS", crsid, "is not valid")
                if not cvx_ext.is_valid:
                    print("WARNING: Extent for CRS", crsid, "is not valid")
                union = extents[crsid].union(cvx_ext);
                if union._geom is not None:
                    extents[crsid] = union
                else:
                    print("WARNING: Dataset", ds.id, "CRS", crsid, "union topology exception, ignoring union")
            if path is not None:
                if sub_r[path]["extents"][crsid] is None:
                    sub_r[path]["extents"][crsid] = cvx_ext
                else:
                    sub_r[path]["extents"][crsid] = sub_r[path]["extents"][crsid].union(cvx_ext)

        ds_count += 1

    r["times"] = sorted(time_set)
    r["time_set"] = time_set
    r["bboxes"] = {crsid: extents[crsid].boundingbox for crsid in crsids}
    if extractor is not None:
        for path in sub_r.keys():
            sub_r[path]["times"] = sorted(sub_r[path]["time_set"])
            sub_r[path]["bboxes"] = {crsid: sub_r[path]["extents"][crsid].boundingbox for crsid in crsids}
            del sub_r[path]["extents"]
        r["sub_products"] = sub_r
    end = datetime.now()
    print("Scanned %d datasets in %d seconds" % (ds_count, (end - start).seconds))
    return r


def determine_ranges(dc):
    ranges = []
    for layer in layer_cfg:
        for product_cfg in layer["products"]:
            ranges.append(determine_product_ranges(dc, product_cfg["product_name"], product_cfg.get("time_zone", 9),
                                                   product_cfg.get("sub_product_extractor")))
    return ranges


def get_sqlconn(dc):
    # TODO: Is this the really the best way to obtain an SQL connection?
    return dc.index._db._engine.connect()


def get_ids_in_db(conn):
    results = conn.execute("select id from wms.product_ranges")
    ids = [r["id"] for r in results]
    return ids


def get_subids_in_db(conn):
    results = conn.execute("select product_id, sub_product_id from wms.sub_product_ranges order by product_id, "
                           "sub_product_id")
    ids = [(r["product_id"], r["sub_product_id"]) for r in results]
    return ids


def rng_update(conn, rng):
    if rng.get("sub_id"):
        conn.execute("""
            UPDATE wms.sub_product_ranges
            SET
                  lat_min=%s,
                  lat_max=%s,
                  lon_min=%s,
                  lon_max=%s,   
                  dates=%s,
                  bboxes=%s
            WHERE product_id=%s
            AND   sub_product_id=%s
                 """,
                     rng["lat"]["min"],
                     rng["lat"]["max"],
                     rng["lon"]["min"],
                     rng["lon"]["max"],

                     Json([t.strftime("%Y-%m-%d") for t in rng["times"]]),
                     Json({crsid: {"top": bbox.top, "bottom": bbox.bottom, "left": bbox.left, "right": bbox.right}
                           for crsid, bbox in rng["bboxes"].items()
                           }),
                     rng["product_id"],
                     rng["sub_id"],
                     )

    else:
        conn.execute("""
            UPDATE wms.product_ranges
            SET
                  lat_min=%s,
                  lat_max=%s,
                  lon_min=%s,
                  lon_max=%s,   
                  dates=%s,
                  bboxes=%s
            WHERE id=%s
                 """,
                     rng["lat"]["min"],
                     rng["lat"]["max"],
                     rng["lon"]["min"],
                     rng["lon"]["max"],

                     Json([t.strftime("%Y-%m-%d") for t in rng["times"]]),
                     Json({crsid: {"top": bbox.top, "bottom": bbox.bottom, "left": bbox.left, "right": bbox.right}
                       for crsid, bbox in rng["bboxes"].items()
                           }),
                     rng["product_id"],
                     )


def rng_insert(conn, rng):
    if rng.get("sub_id"):
        conn.execute("""
                INSERT into wms.sub_product_ranges
                    (product_id, sub_product_id,  lat_min,lat_max,lon_min,lon_max,   dates,bboxes)
                VALUES
                    (%s,%s,   %s,%s,%s,%s,    %s,%s)
                     """,
                     rng["product_id"],
                     rng["sub_id"],

                     rng["lat"]["min"],
                     rng["lat"]["max"],
                     rng["lon"]["min"],
                     rng["lon"]["max"],

                     Json([t.strftime("%Y-%m-%d") for t in rng["times"]]),
                     Json({crsid: {"top": bbox.top, "bottom": bbox.bottom, "left": bbox.left, "right": bbox.right}
                           for crsid, bbox in rng["bboxes"].items()
                           })
                     )
    else:
        conn.execute("""
                INSERT into wms.product_ranges
                    (id,   lat_min,lat_max,lon_min,lon_max,   dates,bboxes)
                VALUES
                    (%s,   %s,%s,%s,%s,    %s,%s)
                     """,
                     rng["product_id"],

                     rng["lat"]["min"],
                     rng["lat"]["max"],
                     rng["lon"]["min"],
                     rng["lon"]["max"],

                     Json([t.strftime("%Y-%m-%d") for t in rng["times"]]),
                     Json({crsid: {"top": bbox.top, "bottom": bbox.bottom, "left": bbox.left, "right": bbox.right}
                           for crsid, bbox in rng["bboxes"].items()
                           })
                     )


def ranges_equal(r1, rdb):
    if r1["product_id"] != rdb["product_id"]:
        return False
    if r1.get("sub_id") != rdb.get("sub_product_id"):
        return False
    for coord in ("lat", "lon"):
        for ext in ("max", "min"):
            if abs(r1[coord][ext] - rdb[coord][ext]) > 1e-12:
                return False
    if len(r1["times"]) != len(rdb["times"]):
        return False
    for t1, t2 in zip_longest(r1["times"], rdb["times"]):
        if t1 != t2:
            return False
    if len(r1["bboxes"]) != len(rdb["bboxes"]):
        return False
    try:
        for cs in r1["bboxes"].keys():
            bb1 = r1["bboxes"][cs]
            bb2 = rdb["bboxes"][cs]
            if abs(bb1.top - float(bb2["top"])) > 1e-12:
                return False
            if abs(bb1.bottom - float(bb2["bottom"])) > 1e-12:
                return False
            if abs(bb1.left - float(bb2["left"])) > 1e-12:
                return False
            if abs(bb1.right - float(bb2["right"])) > 1e-12:
                return False
    except KeyError:
        return False
    return True

def update_range(dc, product):
    def find(list, key, value):
        for d in list:
            if d[key] == value:
                return d
        return None

    products = [find(p["products"], "product_name", product) for p in layer_cfg]
    if products[0] is not None:
        layer = products[0]
        product_range = determine_product_ranges(dc,
                                                 product,
                                                 layer.get("time_zone", 9),
                                                 layer.get("sub_product_extractor"))
        conn = get_sqlconn(dc)
        txn = conn.begin()
        ids_in_db = get_ids_in_db(conn)
        subids_in_db = get_subids_in_db(conn)

        if product_range["product_id"] in ids_in_db:
            db_range = get_ranges(dc, product_range["product_id"])
            if ranges_equal(product_range, db_range):
                print("Ranges equal, not updating")
            else:
                rng_update(conn, product_range)
                print("Updating range")
        else:
            rng_insert(conn, product_range)
            print("Inserting new range")

        if "sub_products" in product_range:
            for path, subr in product_range["sub_products"].items():
                db_range = get_ranges(dc, subr["product_id"], path)
                if (subr["product_id"], path) in subids_in_db:
                    db_range = get_ranges(dc, subr["product_id"], path)
                    if ranges_equal(subr, db_range):
                        pass
                    else:
                        rng_update(conn, subr)
                else:
                    rng_insert(conn, subr)
        txn.commit()
        conn.close()
    else:
        print("Could not find product")


def update_all_ranges(dc):
    ranges = determine_ranges(dc)
    conn = get_sqlconn(dc)
    txn = conn.begin()
    ids_in_db = get_ids_in_db(conn)
    subids_in_db = get_subids_in_db(conn)
    i = 0
    u = 0
    p = 0
    si = 0
    su = 0
    sp = 0
    for prod_ranges in ranges:
        if prod_ranges["product_id"] in ids_in_db:
            db_ranges = get_ranges(dc, prod_ranges["product_id"])
            if ranges_equal(prod_ranges, db_ranges):
                p += 1
            else:
                rng_update(conn, prod_ranges)
                u += 1
        else:
            rng_insert(conn, prod_ranges)
            i += 1
        if "sub_products" in prod_ranges:
            for path, subr in prod_ranges["sub_products"].items():
                db_ranges = get_ranges(dc, subr["product_id"], path)
                if (subr["product_id"], path) in subids_in_db:
                    db_ranges = get_ranges(dc, subr["product_id"], path)
                    if ranges_equal(subr, db_ranges):
                        sp += 1
                    else:
                        rng_update(conn, subr)
                        su += 1
                else:
                    rng_insert(conn, subr)
                    si += 1

    txn.commit()
    conn.close()
    return p, u, i, sp, su, si


def get_ranges(dc, product, path=None):
    if isinstance(product, int):
        product_id = product
    else:
        if isinstance(product, str):
            product = dc.index.products.get_by_name(product)
        product_id = product.id

    conn = get_sqlconn(dc)
    if path is not None:
        results = conn.execute("select * from wms.sub_product_ranges where product_id=%s and sub_product_id=%s",
                               product_id, path)
    else:
        results = conn.execute("select * from wms.product_ranges where id=%s", product_id)
    for result in results:
        conn.close()
        times = [datetime.strptime(d, "%Y-%m-%d").date() for d in result["dates"]]
        return {
            "product_id": product_id,
            "path": path,
            "lat": {
                "min": float(result["lat_min"]),
                "max": float(result["lat_max"]),
            },
            "lon": {
                "min": float(result["lon_min"]),
                "max": float(result["lon_max"]),
            },
            "times": times,
            "start_time": times[0],
            "end_time": times[-1],
            "time_set": set(times),
            "bboxes": result["bboxes"]
        }


def get_sub_ranges(dc, product):
    if isinstance(product, int):
        product_id = product
    else:
        if isinstance(product, str):
            product = dc.index.products.get_by_name(product)
        product_id = product.id

    conn = get_sqlconn(dc)
    results = conn.execute("select sub_product_id from wms.sub_product_ranges where product_id=%s", product_id)
    return {r["sub_product_id"]: get_ranges(dc, product_id, r["sub_product_id"]) for r in results}

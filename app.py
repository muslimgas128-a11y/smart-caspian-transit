
from flask import Flask, request, jsonify, render_template_string
import sqlite3
from datetime import datetime, timedelta
import math
import os
import requests
import networkx as nx
import osmnx as ox
import searoute as sr
from dotenv import load_dotenv

load_dotenv()

ORS_API_KEY = os.getenv("ORS_API_KEY", "").strip()
YANDEX_MAPS_API_KEY = os.getenv("YANDEX_MAPS_API_KEY", "").strip()


app = Flask(__name__)
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transit.db")


# ---------------- DATABASE ----------------

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()

    con.execute("""
        CREATE TABLE IF NOT EXISTS cities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            country TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cargo TEXT NOT NULL,
            origin_id INTEGER NOT NULL,
            destination_id INTEGER NOT NULL,
            transport TEXT NOT NULL,
            weight REAL NOT NULL,
            eta_hours REAL NOT NULL,
            port_load REAL DEFAULT 20,
            weather_risk REAL DEFAULT 10,
            status TEXT DEFAULT 'В пути',
            created_at TEXT NOT NULL
        )
    """)

    if con.execute("SELECT COUNT(*) FROM cities").fetchone()[0] == 0:
        cities = [
            ("Астрахань", "Россия", 46.3497, 48.0408),
            ("Баку", "Азербайджан", 40.4093, 49.8671),
            ("Актау", "Казахстан", 43.6532, 51.1975),
            ("Тегеран", "Иран", 35.6892, 51.3890),
            ("Бендер-Аббас", "Иран", 27.1832, 56.2666),
            ("Мумбаи", "Индия", 19.0760, 72.8777),
            ("Махачкала", "Россия", 42.9849, 47.5047),
        ]
        con.executemany(
            "INSERT INTO cities (name,country,lat,lon) VALUES (?,?,?,?)",
            cities
        )

    if con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
        cities = {
            r["name"]: r["id"]
            for r in con.execute("SELECT id,name FROM cities").fetchall()
        }
        now = datetime.now().isoformat()
        orders = [
            ("Электроника", cities["Баку"], cities["Астрахань"],
             "Море", 120, 36, 65, 20, "В пути", now),
            ("Промышленное оборудование", cities["Актау"], cities["Тегеран"],
             "Мультимодальный", 450, 72, 35, 40, "В пути", now),
            ("Текстиль", cities["Мумбаи"], cities["Бендер-Аббас"],
             "Море", 80, 48, 25, 15, "На таможне", now),
        ]
        con.executemany("""
            INSERT INTO orders
            (cargo,origin_id,destination_id,transport,weight,
             eta_hours,port_load,weather_risk,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, orders)

    con.commit()
    con.close()


# ---------------- DATA HELPERS ----------------

def get_cities():
    con = db()
    rows = con.execute("SELECT * FROM cities ORDER BY name").fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_orders():
    con = db()
    rows = con.execute("""
        SELECT
            o.*,
            a.name AS origin,
            a.country AS origin_country,
            a.lat AS origin_lat,
            a.lon AS origin_lon,
            b.name AS destination,
            b.country AS destination_country,
            b.lat AS destination_lat,
            b.lon AS destination_lon
        FROM orders o
        JOIN cities a ON a.id = o.origin_id
        JOIN cities b ON b.id = o.destination_id
        ORDER BY o.id DESC
    """).fetchall()
    con.close()

    result = []
    for row in rows:
        item = dict(row)
        created = datetime.fromisoformat(item["created_at"])
        arrival = created + timedelta(hours=item["eta_hours"])
        remaining = (arrival - datetime.now()).total_seconds() / 3600

        item["arrival"] = arrival.strftime("%d.%m.%Y %H:%M")
        item["remaining_hours"] = round(max(0, remaining), 1)

        # Демонстрационная модель риска задержки
        risk = (
            item["port_load"] * 0.45 +
            item["weather_risk"] * 0.35
        )
        item["delay_risk"] = round(min(100, risk), 1)

        if item["status"] == "Доставлен":
            item["delay_risk"] = 0

        result.append(item)

    return result


# ---------------- ROUTING ENGINE ----------------

def make_feature(coords, transport):
    return {
        "type": "Feature",
        "properties": {"transport": transport},
        "geometry": {
            "type": "LineString",
            "coordinates": coords
        }
    }


def get_road_route(order):
    if not ORS_API_KEY:
        raise ValueError("Добавь ORS_API_KEY в файл .env")

    url = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"

    response = requests.post(
        url,
        headers={
            "Authorization": ORS_API_KEY,
            "Content-Type": "application/json"
        },
        json={
            "coordinates": [
                [order["origin_lon"], order["origin_lat"]],
                [order["destination_lon"], order["destination_lat"]]
            ]
        },
        timeout=40
    )

    if not response.ok:
        raise ValueError(
            f"Ошибка OpenRouteService: {response.status_code} "
            f"{response.text[:250]}"
        )

    result = response.json()

    if not result.get("features"):
        raise ValueError("Дорожный маршрут не найден")

    feature = result["features"][0]
    feature["properties"]["transport"] = "Авто"

    return feature


def get_sea_route(order):
    route = sr.searoute(
        [order["origin_lon"], order["origin_lat"]],
        [order["destination_lon"], order["destination_lat"]],
        units="km",
        append_orig_dest=True
    )

    if hasattr(route, "__geo_interface__"):
        route = route.__geo_interface__
    elif not isinstance(route, dict):
        route = dict(route)

    route.setdefault("properties", {})
    route["properties"]["transport"] = "Море"

    return route


def get_rail_route(order):
    start_lat = order["origin_lat"]
    start_lon = order["origin_lon"]

    end_lat = order["destination_lat"]
    end_lon = order["destination_lon"]

    margin = 0.35

    north = max(start_lat, end_lat) + margin
    south = min(start_lat, end_lat) - margin
    east = max(start_lon, end_lon) + margin
    west = min(start_lon, end_lon) - margin

    if north - south > 8 or east - west > 12:
        raise ValueError(
            "Слишком большое расстояние для загрузки ж/д сети. "
            "Разбей перевозку на несколько участков."
        )

    graph = ox.graph_from_bbox(
        bbox=(west, south, east, north),
        custom_filter='["railway"~"rail|light_rail|narrow_gauge"]',
        simplify=True,
        retain_all=True
    )

    if graph.number_of_nodes() == 0:
        raise ValueError("Железнодорожная сеть не найдена")

    start_node = ox.distance.nearest_nodes(
        graph, X=start_lon, Y=start_lat
    )

    end_node = ox.distance.nearest_nodes(
        graph, X=end_lon, Y=end_lat
    )

    graph = graph.to_undirected()

    try:
        path = nx.shortest_path(
            graph,
            start_node,
            end_node,
            weight="length"
        )
    except nx.NetworkXNoPath:
        raise ValueError("Железнодорожные участки не соединены")

    coordinates = [
        [graph.nodes[node]["x"], graph.nodes[node]["y"]]
        for node in path
    ]

    return make_feature(coordinates, "Железная дорога")


def get_multimodal_route(order):
    """Демонстрационный мультимодальный маршрут: море до Баку + авто до пункта назначения."""
    con = db()
    baku_row = con.execute(
        "SELECT name,country,lat,lon FROM cities WHERE name LIKE ? ORDER BY id LIMIT 1",
        ("%Баку%",)
    ).fetchone()
    con.close()
    if not baku_row:
        raise ValueError("Для мультимодального маршрута добавьте город Баку")

    baku = dict(baku_row)
    origin = {
        "origin_lat": order["origin_lat"], "origin_lon": order["origin_lon"],
        "destination_lat": baku["lat"], "destination_lon": baku["lon"]
    }
    destination = {
        "origin_lat": baku["lat"], "origin_lon": baku["lon"],
        "destination_lat": order["destination_lat"],
        "destination_lon": order["destination_lon"]
    }

    sea = get_sea_route(origin)
    road = get_road_route(destination)
    sea_coords = sea.get("geometry", {}).get("coordinates", [])
    road_coords = road.get("geometry", {}).get("coordinates", [])
    if not sea_coords or not road_coords:
        raise ValueError("Не удалось построить один из участков мультимодального маршрута")

    # Убираем повторную точку стыка, если координаты совпадают.
    if sea_coords[-1] == road_coords[0]:
        coordinates = sea_coords + road_coords[1:]
    else:
        coordinates = sea_coords + road_coords
    return make_feature(coordinates, "Мультимодальный")


@app.get("/api/route/<int:order_id>")
def api_route(order_id):
    order = next(
        (o for o in get_orders() if o["id"] == order_id),
        None
    )

    if not order:
        return jsonify({"error": "Заказ не найден"}), 404

    try:
        transport = order["transport"]

        if transport == "Авто":
            route = get_road_route(order)

        elif transport == "Море":
            route = get_sea_route(order)

        elif transport == "Железная дорога":
            route = get_rail_route(order)

        elif transport == "Мультимодальный":
            route = get_multimodal_route(order)

        else:
            return jsonify({"error": "Неизвестный тип транспорта"}), 400

        return jsonify(route)

    except Exception as error:
        app.logger.exception("Ошибка построения маршрута")
        return jsonify({"error": str(error)}), 502

# ---------------- API ----------------

@app.get("/api/data")
def api_data():
    orders = get_orders()
    cities = get_cities()

    active = [o for o in orders if o["status"] != "Доставлен"]
    avg_risk = (
        round(sum(o["delay_risk"] for o in active) / len(active), 1)
        if active else 0
    )

    return jsonify({
        "cities": cities,
        "orders": orders,
        "stats": {
            "orders": len(orders),
            "active": len(active),
            "cities": len(cities),
            "risk": avg_risk
        }
    })


@app.post("/api/cities")
def add_city():
    data = request.json or {}

    try:
        name = str(data["name"]).strip()
        country = str(data["country"]).strip()
        lat = float(data["lat"])
        lon = float(data["lon"])

        if not name or not country or len(name) > 100 or len(country) > 100:
            raise ValueError()

        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError()

    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Проверьте данные города"}), 400

    con = db()
    duplicate = con.execute(
        "SELECT id FROM cities WHERE lower(name)=lower(?) AND lower(country)=lower(?)",
        (name, country)
    ).fetchone()
    if duplicate:
        con.close()
        return jsonify({"error": "Такой город уже добавлен"}), 409
    cur = con.execute(
        "INSERT INTO cities (name,country,lat,lon) VALUES (?,?,?,?)",
        (name, country, lat, lon)
    )
    con.commit()
    city_id = cur.lastrowid
    con.close()

    return jsonify({"id": city_id, "message": "Город добавлен"})


@app.delete("/api/cities/<int:city_id>")
def delete_city(city_id):
    con = db()

    used = con.execute("""
        SELECT COUNT(*) FROM orders
        WHERE origin_id=? OR destination_id=?
    """, (city_id, city_id)).fetchone()[0]

    if used:
        con.close()
        return jsonify({
            "error": "Нельзя удалить город, который используется в заказах"
        }), 400

    cur = con.execute("DELETE FROM cities WHERE id=?", (city_id,))
    con.commit()
    con.close()
    if cur.rowcount == 0:
        return jsonify({"error": "Город не найден"}), 404
    return jsonify({"message": "Город удалён"})


@app.post("/api/orders")
def add_order():
    data = request.json or {}

    try:
        cargo = str(data["cargo"]).strip()
        origin = int(data["origin_id"])
        destination = int(data["destination_id"])
        transport = str(data["transport"])
        weight = float(data["weight"])
        eta = float(data["eta_hours"])
        port = float(data.get("port_load", 20))
        weather = float(data.get("weather_risk", 10))

        if not cargo or len(cargo) > 160 or origin == destination:
            raise ValueError()
        if transport not in {"Море", "Железная дорога", "Авто", "Мультимодальный"}:
            raise ValueError()

        if weight <= 0 or eta <= 0:
            raise ValueError()

        if not (0 <= port <= 100 and 0 <= weather <= 100):
            raise ValueError()

    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Проверьте поля заказа"}), 400

    con = db()
    city_ids = {
        r["id"] for r in con.execute("SELECT id FROM cities").fetchall()
    }

    if origin not in city_ids or destination not in city_ids:
        con.close()
        return jsonify({"error": "Выбранный город не найден"}), 400

    cur = con.execute("""
        INSERT INTO orders
        (cargo,origin_id,destination_id,transport,weight,
         eta_hours,port_load,weather_risk,status,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        cargo, origin, destination, transport, weight,
        eta, port, weather, "В пути", datetime.now().isoformat()
    ))

    con.commit()
    order_id = cur.lastrowid
    con.close()

    return jsonify({"id": order_id, "message": "Заказ создан"})


@app.patch("/api/orders/<int:order_id>")
def update_order(order_id):
    data = request.json or {}
    status = data.get("status")

    allowed = ["В пути", "На таможне", "Доставлен", "Задержан"]

    if status not in allowed:
        return jsonify({"error": "Неизвестный статус"}), 400

    con = db()
    cur = con.execute(
        "UPDATE orders SET status=? WHERE id=?",
        (status, order_id)
    )
    con.commit()
    con.close()

    if cur.rowcount == 0:
        return jsonify({"error": "Заказ не найден"}), 404

    return jsonify({"message": "Статус обновлён"})


@app.delete("/api/orders/<int:order_id>")
def delete_order(order_id):
    con = db()
    cur = con.execute("DELETE FROM orders WHERE id=?", (order_id,))
    con.commit()
    con.close()
    if cur.rowcount == 0:
        return jsonify({"error": "Заказ не найден"}), 404
    return jsonify({"message": "Заказ удалён"})


# ---------------- FRONTEND ----------------

PAGE = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Smart Caspian Transit</title>


<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@500;600;700;800&display=swap');
:root {
    --bg:#f5f6f7;
    --surface:#ffffff;
    --surface-soft:#fafafa;
    --panel:#ffffff;
    --panel2:#f4f3f0;
    --border:#e8e7e3;
    --text:#252720;
    --muted:#85877f;
    --gold:#b59a68;
    --gold-dark:#92784d;
    --green:#43856a;
    --orange:#b88645;
    --red:#b85f5f;
    --shadow:0 8px 28px rgba(31,35,28,.045);
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body {
    margin:0;
    background:var(--bg);
    color:var(--text);
    font-family:'DM Sans',Arial,sans-serif;
    -webkit-font-smoothing:antialiased;
}
button,input,select { font:inherit; }
button {
    cursor:pointer;
    border:1px solid transparent;
    border-radius:9px;
    padding:10px 15px;
    color:#fff;
    background:var(--gold);
    font-weight:600;
    transition:background .18s ease,transform .18s ease,box-shadow .18s ease;
}
button:hover { background:var(--gold-dark); box-shadow:0 5px 14px rgba(146,120,77,.16); }
button:active { transform:translateY(1px); }
button:disabled { opacity:.55; cursor:not-allowed; box-shadow:none; }
input,select {
    width:100%; min-width:0; padding:11px 12px;
    border-radius:9px; border:1px solid #e5e4df;
    background:#fff; color:var(--text); outline:none;
    transition:border-color .18s ease,box-shadow .18s ease;
}
input:focus,select:focus { border-color:var(--gold); box-shadow:0 0 0 3px rgba(181,154,104,.13); }
input::placeholder { color:#b1b2ac; }
label { display:block; color:var(--muted); font-size:11px; font-weight:600; letter-spacing:.025em; margin:13px 0 6px; }
.topbar {
    min-height:82px; padding:17px clamp(18px,3vw,42px);
    background:rgba(255,255,255,.94); border-bottom:1px solid var(--border);
    display:flex; align-items:center; justify-content:space-between; gap:16px;
    position:sticky; top:0; z-index:500; backdrop-filter:blur(12px);
}
.logo { font-family:'Manrope',sans-serif; font-size:18px; font-weight:800; letter-spacing:.055em; }
.logo span { color:var(--gold); }
.subtitle { color:var(--muted); font-size:11px; margin-top:4px; letter-spacing:.04em; }
.live { color:var(--green); font-size:11px; font-weight:700; letter-spacing:.08em; white-space:nowrap; }
.layout {
    width:min(1600px,100%); margin:0 auto; padding:26px clamp(14px,2.5vw,34px) 40px;
    display:grid; grid-template-columns:minmax(0,1fr) 350px; gap:20px; align-items:start;
}
.stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:13px; margin-bottom:17px; }
.card,.panel { background:var(--surface); border:1px solid var(--border); border-radius:14px; box-shadow:var(--shadow); }
.stat { padding:18px 19px; min-height:104px; position:relative; overflow:hidden; }
.stat:after { content:''; position:absolute; right:0; top:0; width:3px; height:100%; background:linear-gradient(180deg,#d6c5a4,transparent); opacity:.8; }
.stat small { color:var(--muted); font-size:11px; font-weight:600; }
.stat strong { display:block; font-family:'Manrope',sans-serif; font-size:29px; font-weight:700; margin-top:9px; letter-spacing:-.04em; }
.map-card { overflow:hidden; }
.section-head {
    min-height:58px; padding:14px 18px; display:flex; align-items:center; justify-content:space-between; gap:12px;
    border-bottom:1px solid var(--border); background:linear-gradient(180deg,#fff,#fdfdfc);
}
.section-head h3 { margin:0; font-family:'Manrope',sans-serif; font-size:13px; font-weight:800; letter-spacing:.025em; }
#map { height:470px; background:#e9ece9; }
.side { display:flex; flex-direction:column; gap:15px; min-width:0; }
.panel { padding:17px; }
.panel h3 { margin:0 0 13px; font-family:'Manrope',sans-serif; font-size:13px; font-weight:800; }
.form-grid { display:grid; grid-template-columns:1fr 1fr; gap:0 10px; }
.full { grid-column:1/-1; }
.primary { width:100%; margin-top:14px; }
.secondary { color:#655a47; background:#f2eee6; border-color:#e9e2d4; }
.secondary:hover { color:#fff; background:var(--gold-dark); }
.danger { color:#a44e4e; background:#fbf0ef; border-color:#f1dfdd; }
.danger:hover { color:#fff; background:#a95d59; }
.orders { margin-top:17px; overflow:hidden; }
.order { padding:14px 17px; border-bottom:1px solid #efeee9; cursor:pointer; transition:background .15s ease; }
.order:last-child { border-bottom:0; }
.order:hover,.order.selected { background:#faf8f3; }
.order.selected { box-shadow:inset 3px 0 var(--gold); }
.order-top { display:flex; justify-content:space-between; align-items:center; gap:10px; }
.order-top strong { font-size:12px; font-weight:700; }
.order small { color:var(--muted); font-size:11px; }
.route { font-size:12px; margin:8px 0; color:#4b4d46; }
.status { font-size:10px; font-weight:700; padding:5px 9px; border-radius:20px; background:#f0f0ed; color:#77796f; white-space:nowrap; }
.status.green { color:#39785d; background:#eaf4ee; }
.status.orange { color:#9b7138; background:#f8f0e3; }
.status.red { color:#a94f4f; background:#faecea; }
.detail-line { display:flex; justify-content:space-between; align-items:flex-start; padding:9px 0; border-bottom:1px solid #efeee9; font-size:11px; gap:12px; }
.detail-line span:first-child { color:var(--muted); flex:0 0 auto; }
.detail-line strong { text-align:right; font-weight:700; overflow-wrap:anywhere; }
.empty { color:var(--muted); font-size:12px; line-height:1.6; }
.city-row { display:flex; justify-content:space-between; align-items:center; gap:8px; padding:9px 0; border-bottom:1px solid #efeee9; font-size:11px; }
.city-row:last-child { border-bottom:0; }
.city-row button { padding:5px 9px; font-size:12px; }
.notice { color:var(--muted); font-size:10px; line-height:1.5; }
#toast {
    position:fixed; right:22px; bottom:22px; z-index:2000; max-width:min(420px,calc(100vw - 32px));
    padding:13px 16px; border-radius:11px; color:#fff; background:#34372f;
    box-shadow:0 12px 32px rgba(0,0,0,.16); font-size:12px; opacity:0; transform:translateY(10px);
    pointer-events:none; transition:opacity .2s ease,transform .2s ease;
}
#toast.show { opacity:1; transform:translateY(0); }
#toast.error { background:#994e4b; }
@media(max-width:1050px) { .layout { grid-template-columns:minmax(0,1fr) 320px; } }
@media(max-width:900px) { .layout { grid-template-columns:1fr; } .side { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); align-items:start; } }
@media(max-width:620px) {
    .stats { grid-template-columns:1fr 1fr; gap:9px; }
    .stat { padding:14px; min-height:90px; }
    .stat strong { font-size:24px; }
    .topbar { min-height:70px; padding:13px 15px; }
    .logo { font-size:15px; }
    .subtitle { font-size:9px; }
    .layout { padding:13px 10px 28px; gap:13px; }
    #map { height:360px; }
    .side { display:flex; }
    .section-head { padding:12px 14px; }
    .order { padding:12px 13px; }
}


/* ================= WHITE PREMIUM THEME ================= */
:root {
    --bg:#f4f5f7;
    --panel:#ffffff;
    --panel2:#f7f7f5;
    --border:#e7e5df;
    --text:#202329;
    --muted:#85847f;
    --blue:#b08b50;
    --cyan:#a9854c;
    --green:#34866a;
    --orange:#bd8740;
    --red:#bd5b58;
}
html { scroll-behavior:smooth; }
body {
    background:var(--bg);
    color:var(--text);
    font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
    letter-spacing:-.01em;
}
.topbar {
    position:sticky; top:0; z-index:500;
    padding:19px clamp(16px,3vw,38px);
    background:rgba(255,255,255,.94);
    backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border);
    box-shadow:0 4px 18px rgba(30,30,25,.035);
}
.logo { color:#242520; font-size:19px; letter-spacing:.055em; }
.logo span { color:#a9854c; }
.subtitle { color:#92918b; letter-spacing:.025em; }
.live { color:#44866b; font-weight:650; letter-spacing:.035em; }
.layout {
    width:min(1600px,100%);
    margin:0 auto;
    padding:clamp(14px,2vw,28px);
    grid-template-columns:minmax(0,1fr) minmax(300px,350px);
    gap:20px;
}
.card,.panel {
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:16px;
    box-shadow:0 5px 22px rgba(31,31,24,.035);
}
.stat {
    padding:19px 20px;
    background:linear-gradient(145deg,#fff 0%,#fcfbf8 100%);
    transition:transform .18s ease,box-shadow .18s ease;
}
.stat:hover { transform:translateY(-2px); box-shadow:0 9px 25px rgba(31,31,24,.07); }
.stat small { color:#898780; font-size:12px; font-weight:550; }
.stat strong { color:#252620; font-size:29px; font-weight:700; letter-spacing:-.045em; }
.section-head {
    padding:17px 20px;
    border-bottom:1px solid #eeece7;
}
.section-head h3,.panel h3 { color:#282923; font-weight:680; letter-spacing:-.025em; }
#map { height:490px; background:#eeede8; }
.panel { padding:19px; }
.panel h3 { margin-bottom:15px; }
input,select {
    color:#292a27;
    background:#fbfbfa;
    border:1px solid #e4e2dc;
    border-radius:9px;
    outline:none;
    transition:border-color .15s,box-shadow .15s,background .15s;
}
input::placeholder { color:#b0aea7; }
input:focus,select:focus {
    border-color:#b99a68;
    background:#fff;
    box-shadow:0 0 0 3px rgba(176,139,80,.12);
}
label { color:#85837c; font-weight:600; letter-spacing:.015em; }
button {
    color:#fff;
    background:#a9854c;
    border-radius:9px;
    font-weight:650;
    transition:transform .15s,filter .15s,box-shadow .15s;
}
button:hover { filter:brightness(1.04); box-shadow:0 4px 12px rgba(130,99,48,.16); }
button:active { transform:translateY(1px); }
button:focus-visible { outline:3px solid rgba(176,139,80,.25); outline-offset:2px; }
button.secondary { color:#62543f; background:#f2eee6; border:1px solid #e8dfd0; }
button.danger { color:#a84d49; background:#f8ecea; border:1px solid #f0d9d5; }
.order { border-bottom:1px solid #efeee9; padding:15px 19px; transition:background .15s; }
.order:hover,.order.selected { background:#faf8f3; }
.order strong { color:#292a27; font-weight:680; }
.order small,.empty { color:#929089; }
.route { color:#55564f; }
.status { color:#77766f; background:#f0efeb; font-weight:650; }
.status.green { color:#347a60; background:#eaf4ee; }
.status.orange { color:#9a6b2f; background:#f8f0e2; }
.status.red { color:#a64f4b; background:#f8eae8; }
.detail-line { border-bottom:1px solid #efeee9; color:#353630; }
.detail-line span:first-child { color:#89877f; }
.city-row { border-bottom:1px solid #efeee9; color:#393a34; }
.notice { color:#89877f; }
.side { gap:17px; }
@media(max-width:900px) {
    .layout { grid-template-columns:1fr; }
    .side { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); align-items:start; }
    .side > .panel:last-child { grid-column:1/-1; }
}
@media(max-width:600px) {
    .side { display:flex; }
    .stats { gap:9px; }
    .stat { padding:14px; }
    .stat strong { font-size:24px; }
    #map { height:360px; }
    .section-head { padding:14px; }
}

</style>
</head>

<body>
<header class="topbar">
    <div>
        <div class="logo">SMART <span>CASPIAN</span> TRANSIT</div>
        <div class="subtitle">Predictive logistics monitoring platform</div>
    </div>
    <div class="live">● ПАНЕЛЬ УПРАВЛЕНИЯ</div>
</header>

<main class="layout">
<section>
    <div class="stats">
        <div class="card stat">
            <small>Всего заказов</small>
            <strong id="statOrders">0</strong>
        </div>
        <div class="card stat">
            <small>Активные перевозки</small>
            <strong id="statActive">0</strong>
        </div>
        <div class="card stat">
            <small>Города</small>
            <strong id="statCities">0</strong>
        </div>
        <div class="card stat">
            <small>Средний риск задержки</small>
            <strong id="statRisk">0%</strong>
        </div>
    </div>

    <div class="card map-card">
        <div class="section-head">
            <h3>Карта транспортного коридора</h3>
            <span class="live">TRANSPORT MAP</span>
        </div>
        <div id="map"></div>
    </div>

    <div class="card orders">
        <div class="section-head">
            <h3>Транспортные заказы</h3>
            <button class="secondary" onclick="refreshData()">Обновить</button>
        </div>
        <div id="ordersList"></div>
    </div>
</section>

<aside class="side">
    <div class="panel">
        <h3>Детали перевозки</h3>
        <div id="details" class="empty">
            Выберите заказ в списке или на карте.
        </div>
    </div>

    <div class="panel">
        <h3>Добавить заказ</h3>
        <form id="orderForm">
            <label>Название груза</label>
            <input name="cargo" placeholder="Например, электроника" required>

            <div class="form-grid">
                <div>
                    <label>Откуда</label>
                    <select name="origin_id" id="originSelect" required></select>
                </div>
                <div>
                    <label>Куда</label>
                    <select name="destination_id" id="destinationSelect" required></select>
                </div>
                <div>
                    <label>Вес, тонн</label>
                    <input name="weight" type="number" min="0.1" step="0.1" value="100" required>
                </div>
                <div>
                    <label>ETA, часов</label>
                    <input name="eta_hours" type="number" min="1" value="48" required>
                </div>
                <div>
                    <label>Загруженность порта, %</label>
                    <input name="port_load" type="number" min="0" max="100" value="30">
                </div>
                <div>
                    <label>Погодный риск, %</label>
                    <input name="weather_risk" type="number" min="0" max="100" value="15">
                </div>
                <div class="full">
                    <label>Тип транспорта</label>
                    <select name="transport">
                        <option>Море</option>
                        <option>Железная дорога</option>
                        <option>Авто</option>
                        <option>Мультимодальный</option>
                    </select>
                </div>
            </div>
            <button class="primary" type="submit">+ Создать заказ</button>
        </form>
    </div>

    <div class="panel">
        <h3>Добавить город</h3>
        <form id="cityForm">
            <label>Название</label>
            <input name="name" placeholder="Например, Самарканд" required>
            <label>Страна</label>
            <input name="country" placeholder="Узбекистан" required>
            <div class="form-grid">
                <div>
                    <label>Широта</label>
                    <input name="lat" type="number" step="any" placeholder="39.65" required>
                </div>
                <div>
                    <label>Долгота</label>
                    <input name="lon" type="number" step="any" placeholder="66.96" required>
                </div>
            </div>
            <button class="primary" type="submit">+ Добавить город</button>
        </form>
        <div id="citiesList" style="margin-top:12px"></div>
    </div>
</aside>
</main>

<script src="https://api-maps.yandex.ru/2.1/?apikey=__YANDEX_MAPS_API_KEY__&lang=ru_RU" type="text/javascript"></script>

<div id="toast" role="status" aria-live="polite"></div>
<script>
let toastTimer = null;
function notify(message, isError=false) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.className = isError ? "show error" : "show";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = ""; }, 3200);
}

let cities = [];
let orders = [];
let selectedId = null;
let map = null;
let mapObjects = [];
let mapRenderToken = 0;
let renderToken = 0;

function initMap() {
    const container = document.getElementById("map");
    if (!window.ymaps) {
        container.innerHTML = '<div style="padding:20px;color:#9b5b43">Не удалось загрузить Яндекс Карты. Проверьте YANDEX_MAPS_API_KEY.</div>';
        return false;
    }
    if (map) return true;
    map = new ymaps.Map("map", {
        center: [35, 55],
        zoom: 4,
        controls: ["zoomControl", "typeSelector", "fullscreenControl"]
    }, { suppressMapOpenBlock: true });
    return true;
}

function clearMapObjects() {
    if (!map) return;
    map.geoObjects.removeAll();
    mapObjects = [];
}

async function api(url, method="GET", body=null) {
    const options = {method, headers: {"Accept": "application/json"}};
    if (body !== null) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    const contentType = response.headers.get("content-type") || "";
    let data;
    if (contentType.includes("application/json")) {
        data = await response.json();
    } else {
        const raw = await response.text();
        throw new Error(response.ok
            ? "Сервер вернул неожиданный ответ. Проверьте Flask и адрес API."
            : `Ошибка сервера (${response.status}). ${raw.slice(0, 160)}`);
    }
    if (!response.ok) throw new Error(data.error || `Ошибка запроса (${response.status})`);
    return data;
}

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({
        "&":"&amp;", "<":"&lt;", ">":"&gt;",
        '"':"&quot;", "'":"&#039;"
    })[c]);
}

function statusClass(status) {
    if (status === "Доставлен") return "green";
    if (status === "Задержан") return "red";
    if (status === "На таможне") return "orange";
    return "";
}

async function refreshData() {
    try {
        await loadData();
        notify("Данные обновлены");
    } catch (error) {
        notify(error.message, true);
    }
}

async function loadData() {
    const data = await api("/api/data");

    cities = data.cities;
    orders = data.orders;

    document.getElementById("statOrders").textContent = data.stats.orders;
    document.getElementById("statActive").textContent = data.stats.active;
    document.getElementById("statCities").textContent = data.stats.cities;
    document.getElementById("statRisk").textContent = data.stats.risk + "%";

    renderCities();
    renderOrders();
    renderMap();

    if (selectedId) {
        const order = orders.find(o => o.id === selectedId);
        if (order) selectOrder(order.id);
        else {
            selectedId = null;
            document.getElementById("details").innerHTML =
                '<div class="empty">Выберите заказ в списке.</div>';
        }
    }
}

function renderCities() {
    const originSelect = document.getElementById("originSelect");
    const destinationSelect = document.getElementById("destinationSelect");
    const oldOrigin = originSelect.value;
    const oldDestination = destinationSelect.value;
    const options = cities.map(c =>
        `<option value="${c.id}">${escapeHtml(c.name)}, ${escapeHtml(c.country)}</option>`
    ).join("");

    originSelect.innerHTML = options || '<option value="">Сначала добавьте город</option>';
    destinationSelect.innerHTML = options || '<option value="">Сначала добавьте город</option>';
    if (cities.some(c => String(c.id) === oldOrigin)) originSelect.value = oldOrigin;
    if (cities.some(c => String(c.id) === oldDestination)) destinationSelect.value = oldDestination;
    document.querySelector('#orderForm button[type="submit"]').disabled = cities.length < 2;

    document.getElementById("citiesList").innerHTML = cities.map(c => `
        <div class="city-row">
            <span>${escapeHtml(c.name)} <small>(${escapeHtml(c.country)})</small></span>
            <button class="danger" onclick="deleteCity(${c.id})">×</button>
        </div>
    `).join("");
}

function renderOrders() {
    const container = document.getElementById("ordersList");

    if (!orders.length) {
        container.innerHTML = '<div class="order empty">Заказов пока нет.</div>';
        return;
    }

    container.innerHTML = orders.map(o => `
        <div class="order ${selectedId === o.id ? "selected" : ""}"
             onclick="selectOrder(${o.id})">
            <div class="order-top">
                <strong>#${o.id} · ${escapeHtml(o.cargo)}</strong>
                <span class="status ${statusClass(o.status)}">${escapeHtml(o.status)}</span>
            </div>
            <div class="route">
                ${escapeHtml(o.origin)} → ${escapeHtml(o.destination)}
            </div>
            <small>${escapeHtml(o.transport)} · ${o.weight} тонн · ETA ${o.eta_hours} ч.</small>
        </div>
    `).join("");
}


async function renderMap() {
    if (!map) return;
    const token = ++renderToken;
    clearMapObjects();

    cities.forEach(city => {
        const marker = new ymaps.Placemark(
            [Number(city.lat), Number(city.lon)],
            {
                balloonContentHeader: escapeHtml(city.name),
                balloonContentBody: escapeHtml(city.country),
                hintContent: `${escapeHtml(city.name)}, ${escapeHtml(city.country)}`
            },
            { preset: "islands#blueCircleDotIcon" }
        );
        map.geoObjects.add(marker);
        mapObjects.push(marker);
    });

    const colors = {
        "Море": "#4c9fba",
        "Железная дорога": "#b28b50",
        "Авто": "#718ba7",
        "Мультимодальный": "#927cad"
    };

    for (const order of orders) {
        if (token !== renderToken) return;
        try {
            const feature = await api(`/api/route/${order.id}`);
            if (token !== renderToken) return;
            const coords = feature?.geometry?.coordinates;
            if (!Array.isArray(coords) || coords.length < 2) continue;

            // GeoJSON uses [longitude, latitude]; Yandex Maps uses [latitude, longitude].
            const points = coords
                .filter(point => Array.isArray(point) && point.length >= 2 &&
                    Number.isFinite(Number(point[0])) && Number.isFinite(Number(point[1])))
                .map(point => [Number(point[1]), Number(point[0])]);
            if (points.length < 2) continue;

            const line = new ymaps.Polyline(points, {
                balloonContentHeader: `Заказ #${order.id}`,
                balloonContentBody: `${escapeHtml(order.origin)} → ${escapeHtml(order.destination)}<br>${escapeHtml(order.transport)}`
            }, {
                strokeColor: order.id === selectedId ? "#b59a68" : (colors[order.transport] || "#718ba7"),
                strokeWidth: order.id === selectedId ? 6 : 4,
                strokeOpacity: order.id === selectedId ? 0.95 : 0.78
            });
            line.events.add("click", () => selectOrder(order.id));
            map.geoObjects.add(line);
            mapObjects.push(line);
        } catch (error) {
            console.warn(`Маршрут #${order.id} недоступен: ${error.message}`);
        }
    }
}

function selectOrder(id) {
    const o = orders.find(x => x.id === id);
    if (!o) return;

    selectedId = id;
    renderOrders();
    renderMap();

    const riskColor = o.delay_risk >= 60 ? "#b85f5f" :
                      o.delay_risk >= 30 ? "#b88645" : "#43856a";

    document.getElementById("details").innerHTML = `
        <div class="detail-line"><span>Номер заказа</span><strong>#${o.id}</strong></div>
        <div class="detail-line"><span>Груз</span><strong>${escapeHtml(o.cargo)}</strong></div>
        <div class="detail-line"><span>Маршрут</span><strong>${escapeHtml(o.origin)} → ${escapeHtml(o.destination)}</strong></div>
        <div class="detail-line"><span>Транспорт</span><strong>${escapeHtml(o.transport)}</strong></div>
        <div class="detail-line"><span>Вес</span><strong>${o.weight} тонн</strong></div>
        <div class="detail-line"><span>Осталось времени</span><strong>${o.remaining_hours} ч.</strong></div>
        <div class="detail-line"><span>Ожидаемое прибытие</span><strong>${o.arrival}</strong></div>
        <div class="detail-line"><span>Риск задержки</span><strong style="color:${riskColor}">${o.delay_risk}%</strong></div>
        <div class="detail-line"><span>Загруженность порта</span><strong>${o.port_load}%</strong></div>
        <div class="detail-line"><span>Погодный риск</span><strong>${o.weather_risk}%</strong></div>

        <label>Изменить статус</label>
        <select id="statusSelect">
            ${["В пути","На таможне","Доставлен","Задержан"].map(s =>
                `<option ${s === o.status ? "selected" : ""}>${s}</option>`
            ).join("")}
        </select>
        <button class="primary" onclick="changeStatus(${o.id})">Сохранить статус</button>
        <button class="primary danger" onclick="deleteOrder(${o.id})">Удалить заказ</button>
    `;

    if (map) {
        const bounds = [
            [Number(o.origin_lat), Number(o.origin_lon)],
            [Number(o.destination_lat), Number(o.destination_lon)]
        ];
        map.setBounds(bounds, { checkZoomRange: true, zoomMargin: 50 }).catch(() => {});
    }
}

async function changeStatus(id) {
    try {
        const status = document.getElementById("statusSelect").value;
        await api(`/api/orders/${id}`, "PATCH", {status});
        await loadData();
        notify("Статус заказа обновлён");
    } catch(e) { notify(e.message, true); }
}

async function deleteOrder(id) {
    if (!confirm("Удалить этот заказ?")) return;
    try {
        await api(`/api/orders/${id}`, "DELETE");
        selectedId = null;
        document.getElementById("details").innerHTML =
            '<div class="empty">Выберите заказ в списке или на карте.</div>';
        await loadData();
        notify("Заказ удалён");
    } catch(e) { notify(e.message, true); }
}

async function deleteCity(id) {
    if (!confirm("Удалить город?")) return;
    try {
        await api(`/api/cities/${id}`, "DELETE");
        await loadData();
        notify("Город удалён");
    } catch(e) { notify(e.message, true); }
}

document.getElementById("orderForm").addEventListener("submit", async e => {
    e.preventDefault();
    const form = Object.fromEntries(new FormData(e.target));
    form.origin_id = Number(form.origin_id);
    form.destination_id = Number(form.destination_id);
    form.weight = Number(form.weight);
    form.eta_hours = Number(form.eta_hours);
    form.port_load = Number(form.port_load);
    form.weather_risk = Number(form.weather_risk);
    if (!form.origin_id || !form.destination_id || cities.length < 2) {
        notify("Добавьте минимум два города и выберите маршрут", true);
        return;
    }
    if (form.origin_id === form.destination_id) {
        notify("Город отправления и назначения должны отличаться", true);
        return;
    }

    try {
        const result = await api("/api/orders", "POST", form);
        e.target.reset();
        await loadData();
        selectOrder(result.id);
        notify("Заказ успешно создан");
    } catch(err) { notify(err.message, true); }
});

document.getElementById("cityForm").addEventListener("submit", async e => {
    e.preventDefault();
    const form = Object.fromEntries(new FormData(e.target));
    form.lat = Number(form.lat);
    form.lon = Number(form.lon);

    try {
        await api("/api/cities", "POST", form);
        e.target.reset();
        await loadData();
        notify("Город добавлен");
    } catch(err) { notify(err.message, true); }
});

if (window.ymaps) {
    ymaps.ready(() => {
        if (initMap()) {
            loadData().catch(error => notify(`Не удалось загрузить данные: ${error.message}`, true));
        }
    });
} else {
    document.getElementById("map").innerHTML =
        '<div style="padding:20px;color:#9b5b43">Яндекс Карты не загрузились. Проверьте YANDEX_MAPS_API_KEY.</div>';
    loadData().catch(error => notify(`Не удалось загрузить данные: ${error.message}`, true));
}
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return PAGE.replace("__YANDEX_MAPS_API_KEY__", YANDEX_MAPS_API_KEY)


# Инициализация выполняется и при запуске через gunicorn.
init_db()

if __name__ == "__main__":
    app.run(
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5001"))
    )
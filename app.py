
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
DB = "transit.db"


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

        else:
            return jsonify({
                "error": "Мультимодальный маршрут пока требует настройки отдельных участков"
            }), 400

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
    data = request.get_json(silent=True) or {}

    try:
        name = str(data["name"]).strip()
        country = str(data["country"]).strip()
        lat = float(data["lat"])
        lon = float(data["lon"])

        if not name or not country:
            raise ValueError()

        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError()

    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Проверьте данные города"}), 400

    con = db()
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

    con.execute("DELETE FROM cities WHERE id=?", (city_id,))
    con.commit()
    con.close()

    return jsonify({"message": "Город удалён"})


@app.post("/api/orders")
def add_order():
    data = request.get_json(silent=True) or {}

    try:
        cargo = str(data["cargo"]).strip()
        origin = int(data["origin_id"])
        destination = int(data["destination_id"])
        transport = str(data["transport"])
        weight = float(data["weight"])
        eta = float(data["eta_hours"])
        port = float(data.get("port_load", 20))
        weather = float(data.get("weather_risk", 10))

        if not cargo or origin == destination:
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
    data = request.get_json(silent=True) or {}
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
    con.execute("DELETE FROM orders WHERE id=?", (order_id,))
    con.commit()
    con.close()
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
:root {
    --bg:#080d18;
    --panel:#111a2b;
    --panel2:#172338;
    --border:#263650;
    --text:#edf4ff;
    --muted:#8495ae;
    --blue:#3987ff;
    --cyan:#36d5e8;
    --green:#36d99a;
    --orange:#ffb454;
    --red:#ff647c;
}
* { box-sizing:border-box; }
body {
    margin:0;
    background:var(--bg);
    color:var(--text);
    font-family:Inter,Arial,sans-serif;
}
button,input,select { font:inherit; }
button {
    cursor:pointer;
    border:0;
    border-radius:9px;
    padding:10px 14px;
    color:white;
    background:var(--blue);
}
button:hover { filter:brightness(1.15); }
input,select {
    width:100%;
    padding:10px;
    border-radius:8px;
    border:1px solid var(--border);
    background:#0b1423;
    color:white;
}
label {
    display:block;
    color:var(--muted);
    font-size:12px;
    margin:12px 0 6px;
}
.topbar {
    padding:18px 25px;
    border-bottom:1px solid var(--border);
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:15px;
}
.logo { font-size:20px; font-weight:800; }
.logo span { color:var(--cyan); }
.subtitle { color:var(--muted); font-size:12px; margin-top:5px; }
.live { color:var(--green); font-size:12px; }
.layout {
    padding:20px;
    display:grid;
    grid-template-columns:minmax(0,1fr) 340px;
    gap:18px;
}
.stats {
    display:grid;
    grid-template-columns:repeat(4,1fr);
    gap:12px;
    margin-bottom:15px;
}
.card,.panel {
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:14px;
}
.stat { padding:16px; }
.stat small { color:var(--muted); }
.stat strong {
    display:block;
    font-size:27px;
    margin-top:9px;
}
.map-card { overflow:hidden; }
.section-head {
    padding:15px 17px;
    display:flex;
    align-items:center;
    justify-content:space-between;
    border-bottom:1px solid var(--border);
}
.section-head h3 { margin:0; font-size:15px; }
#map { height:470px; background:#0d1a2b; }
.side { display:flex; flex-direction:column; gap:15px; }
.panel { padding:16px; }
.panel h3 { margin:0 0 12px; font-size:15px; }
.form-grid {
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:0 10px;
}
.full { grid-column:1/-1; }
.primary { width:100%; margin-top:13px; }
.secondary { background:var(--panel2); }
.danger { background:#6f293a; }
.orders { margin-top:17px; }
.order {
    padding:13px;
    border-bottom:1px solid var(--border);
    cursor:pointer;
}
.order:hover,.order.selected { background:#1b2c46; }
.order-top {
    display:flex;
    justify-content:space-between;
    gap:10px;
}
.order small { color:var(--muted); }
.route { font-size:13px; margin:9px 0; }
.status {
    font-size:11px;
    padding:5px 8px;
    border-radius:20px;
    background:#243750;
    white-space:nowrap;
}
.status.green { color:var(--green); }
.status.orange { color:var(--orange); }
.status.red { color:var(--red); }
.detail-line {
    display:flex;
    justify-content:space-between;
    padding:9px 0;
    border-bottom:1px solid var(--border);
    font-size:13px;
    gap:10px;
}
.detail-line span:first-child { color:var(--muted); }
.detail-line strong { text-align:right; }
.empty { color:var(--muted); font-size:13px; }
.city-row {
    display:flex;
    justify-content:space-between;
    gap:8px;
    padding:8px 0;
    border-bottom:1px solid var(--border);
    font-size:13px;
}
.city-row button { padding:5px 8px; }
.notice { color:var(--muted); font-size:11px; line-height:1.5; }
@media(max-width:900px) {
    .layout { grid-template-columns:1fr; }
}
@media(max-width:600px) {
    .stats { grid-template-columns:1fr 1fr; }
    .topbar { padding:15px; }
    .layout { padding:10px; }
    #map { height:350px; }
}
</style>
</head>

<body>
<header class="topbar">
    <div>
        <div class="logo">SMART <span>CASPIAN</span> TRANSIT</div>
        <div class="subtitle">Predictive logistics monitoring platform</div>
    </div>
    <div class="live">● SYSTEM ONLINE</div>
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
            <span class="live">● LIVE MAP</span>
        </div>
        <div id="map"></div>
    </div>

    <div class="card orders">
        <div class="section-head">
            <h3>Транспортные заказы</h3>
            <button class="secondary" onclick="loadData()">Обновить</button>
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

<script>
let map = null;
let mapObjects = [];

function initMap() {
    if (!window.ymaps) {
        document.getElementById("map").innerHTML =
            '<div style="padding:20px;color:#ffb454">Не удалось загрузить Яндекс Карты. Проверьте YANDEX_MAPS_API_KEY в настройках Render.</div>';
        return;
    }

    map = new ymaps.Map("map", {
        center: [35, 55],
        zoom: 4,
        controls: ["zoomControl", "typeSelector", "fullscreenControl"]
    }, {
        suppressMapOpenBlock: true
    });
}

function clearMapObjects() {
    if (!map) return;
    map.geoObjects.removeAll();
    mapObjects = [];
}

let cities = [];
let orders = [];
let markers = [];
let routeLayer = null;
let selectedId = null;

async function api(url, method="GET", body=null) {
    const options = {method, headers: {}};
    if (body) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
    }

    const response = await fetch(url, options);
    const raw = await response.text();
    let data;

    try {
        data = JSON.parse(raw);
    } catch (error) {
        throw new Error(
            `Сервер вернул не JSON (HTTP ${response.status}). ` +
            `Проверь адрес API и журнал сервера. ${raw.slice(0, 180)}`
        );
    }

    if (!response.ok) throw new Error(data.error || "Ошибка запроса");
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
    const options = cities.map(c =>
        `<option value="${c.id}">${escapeHtml(c.name)}, ${escapeHtml(c.country)}</option>`
    ).join("");

    document.getElementById("originSelect").innerHTML = options;
    document.getElementById("destinationSelect").innerHTML = options;

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
        "Море": "#36d5e8",
        "Железная дорога": "#ffb454",
        "Авто": "#3987ff",
        "Мультимодальный": "#a78bfa"
    };

    for (const order of orders) {
        try {
            const feature = await api(`/api/route/${order.id}`);
            if (!feature.geometry || !feature.geometry.coordinates) continue;

            // GeoJSON хранит координаты как [долгота, широта], Яндекс — [широта, долгота].
            const points = feature.geometry.coordinates.map(
                point => [Number(point[1]), Number(point[0])]
            );

            const line = new ymaps.Polyline(points, {
                balloonContentHeader: `Заказ #${order.id}`,
                balloonContentBody:
                    `${escapeHtml(order.origin)} → ${escapeHtml(order.destination)}<br>` +
                    `${escapeHtml(order.transport)}`
            }, {
                strokeColor: order.id === selectedId
                    ? "#ffffff"
                    : colors[order.transport] || "#3987ff",
                strokeWidth: order.id === selectedId ? 6 : 4,
                strokeOpacity: 0.85
            });

            line.events.add("click", () => selectOrder(order.id));
            map.geoObjects.add(line);
            mapObjects.push(line);
        } catch (error) {
            console.error(`Ошибка маршрута #${order.id}:`, error.message);
        }
    }
}


function selectOrder(id) {
    const o = orders.find(x => x.id === id);
    if (!o) return;

    selectedId = id;
    renderOrders();
    renderMap();

    const riskColor = o.delay_risk >= 60 ? "#ff647c" :
                      o.delay_risk >= 30 ? "#ffb454" : "#36d99a";

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

    const bounds = [
        [o.origin_lat, o.origin_lon],
        [o.destination_lat, o.destination_lon]
    ];
    if (map) {
        map.setBounds(bounds, { checkZoomRange: true, zoomMargin: 50 }).catch(() => {});
    }
}

async function changeStatus(id) {
    try {
        const status = document.getElementById("statusSelect").value;
        await api(`/api/orders/${id}`, "PATCH", {status});
        await loadData();
    } catch(e) { alert(e.message); }
}

async function deleteOrder(id) {
    if (!confirm("Удалить этот заказ?")) return;
    try {
        await api(`/api/orders/${id}`, "DELETE");
        selectedId = null;
        await loadData();
    } catch(e) { alert(e.message); }
}

async function deleteCity(id) {
    if (!confirm("Удалить город?")) return;
    try {
        await api(`/api/cities/${id}`, "DELETE");
        await loadData();
    } catch(e) { alert(e.message); }
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

    try {
        const result = await api("/api/orders", "POST", form);
        e.target.reset();
        await loadData();
        selectOrder(result.id);
    } catch(err) { alert(err.message); }
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
    } catch(err) { alert(err.message); }
});

if (window.ymaps) {
    ymaps.ready(() => {
        initMap();
        loadData().catch(error => alert(error.message));
    });
} else {
    document.getElementById("map").innerHTML =
        '<div style="padding:20px;color:#ffb454">Яндекс Карты не загрузились. Добавьте YANDEX_MAPS_API_KEY в настройки окружения.</div>';
    loadData().catch(error => alert(error.message));
}
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return PAGE.replace("__YANDEX_MAPS_API_KEY__", YANDEX_MAPS_API_KEY)


# Initialize the database both for local runs and production WSGI servers (Gunicorn).
init_db()

if __name__ == "__main__":
    app.run(
        debug=False,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5001"))
    )
import os
import time
import random
import logging
import uuid
import threading

from flask import Flask, redirect, render_template, request, send_from_directory, url_for, jsonify, session
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace, metrics

# Configure Azure Monitor OpenTelemetry
configure_azure_monitor()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== SIMULATED USER POOL ==========
# Demo personas to generate realistic multi-user telemetry
DEMO_USERS = [
    {"id": "alice@contoso.com",  "name": "Alice Chen",     "role": "SRE Engineer"},
    {"id": "bob@contoso.com",    "name": "Bob Martinez",   "role": "DevOps Lead"},
    {"id": "carol@contoso.com",  "name": "Carol Wu",       "role": "Platform Engineer"},
    {"id": "dave@contoso.com",   "name": "Dave Singh",     "role": "Cloud Architect"},
    {"id": "eve@contoso.com",    "name": "Eve Johnson",    "role": "Backend Developer"},
    {"id": "frank@contoso.com",  "name": "Frank Kim",      "role": "Site Reliability Eng"},
    {"id": "grace@contoso.com",  "name": "Grace Patel",    "role": "Infra Engineer"},
    {"id": "hiro@contoso.com",   "name": "Hiro Tanaka",    "role": "Ops Manager"},
]

# ========== CUSTOM TELEMETRY SETUP ==========

# 1. CUSTOM TRACER — creates custom spans (distributed traces) in App Insights
#    These show up as "dependencies" or custom trace operations
tracer = trace.get_tracer("sre-demo-flask.inventory")

# 2. CUSTOM METRICS — creates custom counters/histograms in App Insights
#    These show up under "customMetrics" in App Insights
meter = metrics.get_meter("sre-demo-flask.inventory")

# Business metrics: counters and histograms
purchase_counter = meter.create_counter(
    name="inventory.purchases.count",
    description="Total number of purchase attempts",
    unit="1"
)
purchase_revenue = meter.create_counter(
    name="inventory.purchases.revenue",
    description="Total revenue from successful purchases",
    unit="USD"
)
stock_alert_counter = meter.create_counter(
    name="inventory.stock.alerts",
    description="Low stock and out-of-stock alerts",
    unit="1"
)
api_latency_histogram = meter.create_histogram(
    name="inventory.api.latency",
    description="API endpoint latency",
    unit="ms"
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "sre-demo-secret-key-2024")

# ========== CHAOS ENDPOINT CONFIGURATION ==========
ENABLE_CHAOS_ENDPOINTS = os.environ.get("ENABLE_CHAOS_ENDPOINTS", "false").lower() == "true"
CHAOS_COOLDOWN_SECONDS = 60
_chaos_last_called: dict = {}  # endpoint -> last invocation timestamp
_chaos_lock = threading.Lock()  # protects _chaos_last_called in multi-threaded deployments

# Explicitly instrument this Flask app instance for request telemetry
from opentelemetry.instrumentation.flask import FlaskInstrumentor
FlaskInstrumentor().instrument_app(app)


# ========== USER ID TRACKING MIDDLEWARE ==========
@app.before_request
def set_user_context():
    """Assign a demo user and stamp every span with enduser.id for App Insights."""
    # Priority: X-User-Id header > session cookie > random assignment
    user_id = request.headers.get("X-User-Id")
    if user_id:
        # Look up from pool or create ad-hoc
        user = next((u for u in DEMO_USERS if u["id"] == user_id), None)
        if not user:
            user = {"id": user_id, "name": user_id.split("@")[0].title(), "role": "API User"}
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["user_role"] = user["role"]
    elif "user_id" not in session:
        user = random.choice(DEMO_USERS)
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["user_role"] = user["role"]
        session["session_id"] = str(uuid.uuid4())[:8]

    # Stamp the current OpenTelemetry span with user identity
    span = trace.get_current_span()
    if span and span.is_recording():
        span.set_attribute("enduser.id", session["user_id"])
        span.set_attribute("enduser.name", session.get("user_name", ""))
        span.set_attribute("enduser.role", session.get("user_role", ""))


# PostgreSQL connection via SQLAlchemy
from flask_sqlalchemy import SQLAlchemy

DB_URI = os.environ.get("DATABASE_URL",
    "postgresql://pgadmin:SreFlask2026!160@sre-demo-pg.postgres.database.azure.com:5432/sre_inventory_db?sslmode=require")
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# ========== MODELS ==========
class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, default=0)
    status = db.Column(db.String(50), default="in_stock")
    created_at = db.Column(db.DateTime, default=db.func.now())

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "category": self.category,
            "price": self.price, "stock": self.stock, "status": self.status,
            "created_at": str(self.created_at)
        }


# Seed database on startup
with app.app_context():
    db.create_all()
    if Product.query.count() == 0:
        seeds = [
            Product(name="Kubernetes Cluster License", category="Infrastructure", price=2999.99, stock=50, status="in_stock"),
            Product(name="SSL Certificate (Wildcard)", category="Security", price=199.99, stock=200, status="in_stock"),
            Product(name="Managed PostgreSQL", category="Database", price=450.00, stock=30, status="in_stock"),
            Product(name="CDN Bandwidth 10TB", category="Networking", price=899.00, stock=5, status="low_stock"),
            Product(name="AI Compute GPU Hours", category="Compute", price=3500.00, stock=2, status="low_stock"),
            Product(name="Object Storage 1PB", category="Storage", price=12000.00, stock=0, status="out_of_stock"),
            Product(name="DDoS Protection Plan", category="Security", price=2999.00, stock=100, status="in_stock"),
            Product(name="Log Analytics Workspace", category="Monitoring", price=150.00, stock=80, status="in_stock"),
        ]
        db.session.add_all(seeds)
        db.session.commit()
        logger.info("Seeded %d products into database", len(seeds))


# ========== ORIGINAL ROUTES ==========
@app.route('/')
def index():
    logger.info('Request for index page received')
    return render_template('index.html')


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')


@app.route('/hello', methods=['POST'])
def hello():
    name = request.form.get('name')
    if name:
        logger.info('Request for hello page received with name=%s', name)
        return render_template('hello.html', name=name)
    else:
        return redirect(url_for('index'))


# ========== CRUD: LIST PRODUCTS ==========
@app.route('/api/products')
def list_products():
    logger.info('Fetching all products from database')
    products = Product.query.order_by(Product.created_at.desc()).all()
    return jsonify([p.to_dict() for p in products])


# ========== CRUD: GET PRODUCT ==========
@app.route('/api/products/<int:product_id>')
def get_product(product_id):
    logger.info('Fetching product %d', product_id)
    product = db.session.get(Product, product_id)
    if not product:
        logger.warning('Product %d not found', product_id)
        return jsonify({"error": "Product not found"}), 404
    return jsonify(product.to_dict())


# ========== CRUD: CREATE PRODUCT ==========
@app.route('/api/products', methods=['POST'])
def create_product():
    data = request.get_json()
    logger.info('Creating product: %s', data.get('name'))
    product = Product(
        name=data['name'], category=data.get('category', 'General'),
        price=data['price'], stock=data.get('stock', 0),
        status="in_stock" if data.get('stock', 0) > 0 else "out_of_stock"
    )
    db.session.add(product)
    db.session.commit()
    logger.info('Product %d created successfully', product.id)
    return jsonify(product.to_dict()), 201


# ========== CRUD: UPDATE PRODUCT ==========
@app.route('/api/products/<int:product_id>', methods=['PUT'])
def update_product(product_id):
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404

    data = request.get_json()
    if 'stock' in data:
        product.stock = data['stock']
        product.status = "in_stock" if data['stock'] > 10 else ("low_stock" if data['stock'] > 0 else "out_of_stock")
    if 'price' in data:
        product.price = data['price']
    if 'name' in data:
        product.name = data['name']
    db.session.commit()
    logger.info('Product %d updated: stock=%d, status=%s', product_id, product.stock, product.status)
    return jsonify(product.to_dict())


# ========== CRUD: DELETE PRODUCT ==========
@app.route('/api/products/<int:product_id>', methods=['DELETE'])
def delete_product(product_id):
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404
    db.session.delete(product)
    db.session.commit()
    logger.warning('Product %d deleted', product_id)
    return jsonify({"message": "Deleted", "product_id": product_id})


# ========== SEARCH / AGGREGATE ==========
@app.route('/api/products/search')
def search_products():
    category = request.args.get('category')
    status = request.args.get('status')
    logger.info('Searching products: category=%s, status=%s', category, status)
    query = Product.query
    if category:
        query = query.filter(Product.category == category)
    if status:
        query = query.filter(Product.status == status)
    return jsonify([p.to_dict() for p in query.all()])


@app.route('/api/products/stats')
def product_stats():
    logger.info('Computing product statistics from database')
    from sqlalchemy import func
    total = Product.query.count()
    total_value = db.session.query(func.sum(Product.price * Product.stock)).scalar() or 0
    by_status = db.session.query(
        Product.status, func.count(Product.id), func.sum(Product.price * Product.stock)
    ).group_by(Product.status).all()
    return jsonify({
        "total_products": total,
        "total_inventory_value": round(float(total_value), 2),
        "by_status": [{"status": s, "count": c, "value": round(float(v or 0), 2)} for s, c, v in by_status]
    })


# ========== PURCHASE (error-prone) — WITH CUSTOM TELEMETRY ==========
@app.route('/api/products/<int:product_id>/purchase', methods=['POST'])
def purchase_product(product_id):
    start_time = time.time()

    # 3. CUSTOM SPAN — wraps business logic in a named trace operation
    #    Shows up as a custom dependency/operation in App Insights "Transaction search"
    with tracer.start_as_current_span("process-purchase") as span:
        # Set custom attributes on the span (visible in App Insights trace details)
        span.set_attribute("product.id", product_id)
        span.set_attribute("purchase.source", request.headers.get("User-Agent", "unknown"))

        product = db.session.get(Product, product_id)
        if not product:
            span.set_attribute("purchase.result", "product_not_found")
            purchase_counter.add(1, {"status": "not_found", "product_id": str(product_id)})
            return jsonify({"error": "Product not found"}), 404

        qty = request.get_json().get('quantity', 1) if request.is_json else 1
        span.set_attribute("product.name", product.name)
        span.set_attribute("product.category", product.category)
        span.set_attribute("purchase.quantity", qty)
        span.set_attribute("product.price", product.price)
        span.set_attribute("product.stock_before", product.stock)

        # 4. CUSTOM METRIC — count every purchase attempt with dimensional attributes
        purchase_counter.add(1, {"status": "attempted", "category": product.category})

        roll = random.randint(1, 100)
        if roll <= 2:  # 2% payment timeout rate (reduced from 20%)
            span.set_attribute("purchase.result", "payment_timeout")
            span.set_status(trace.StatusCode.ERROR, "Payment gateway timeout")
            purchase_counter.add(1, {"status": "failed", "reason": "timeout", "category": product.category})

            # 5. STRUCTURED BUSINESS LOG — with custom dimensions (extra= dict)
            #    Shows up as AppTraces with customDimensions in App Insights
            logger.error('Purchase failed: payment gateway timeout',
                         extra={"custom_dimensions": {
                             "product_id": product_id, "product_name": product.name,
                             "category": product.category, "quantity": qty,
                             "failure_reason": "payment_gateway_timeout",
                             "transaction_value": round(product.price * qty, 2)
                         }})
            raise TimeoutError(f"Payment gateway timeout for product {product_id}")

        if roll == 3:  # 1% inventory sync error rate (reduced from 10%)
            span.set_attribute("purchase.result", "inventory_sync_error")
            span.set_status(trace.StatusCode.ERROR, "Inventory sync failed")
            purchase_counter.add(1, {"status": "failed", "reason": "sync_error", "category": product.category})

            logger.error('Purchase failed: inventory sync error',
                         extra={"custom_dimensions": {
                             "product_id": product_id, "product_name": product.name,
                             "category": product.category, "failure_reason": "stale_cache"
                         }})
            raise RuntimeError(f"Inventory sync failed — stale cache for product {product_id}")

        if product.stock < qty:
            span.set_attribute("purchase.result", "insufficient_stock")
            purchase_counter.add(1, {"status": "failed", "reason": "insufficient_stock", "category": product.category})
            stock_alert_counter.add(1, {"alert_type": "insufficient_stock", "product_name": product.name})

            logger.warning('Insufficient stock for purchase',
                           extra={"custom_dimensions": {
                               "product_id": product_id, "product_name": product.name,
                               "requested_qty": qty, "available_stock": product.stock,
                               "shortage": qty - product.stock
                           }})
            return jsonify({"error": "Insufficient stock", "available": product.stock}), 400

        # Successful purchase
        product.stock -= qty
        old_status = product.status
        product.status = "in_stock" if product.stock > 10 else ("low_stock" if product.stock > 0 else "out_of_stock")
        db.session.commit()

        revenue = round(product.price * qty, 2)
        span.set_attribute("purchase.result", "success")
        span.set_attribute("purchase.revenue", revenue)
        span.set_attribute("product.stock_after", product.stock)
        span.set_attribute("product.status_after", product.status)

        # 6. CUSTOM REVENUE METRIC — tracks actual dollar amount
        purchase_revenue.add(revenue, {"category": product.category, "product_name": product.name})
        purchase_counter.add(1, {"status": "success", "category": product.category})

        # 7. STOCK ALERT METRIC — fires when stock drops below threshold
        if product.status == "low_stock" and old_status != "low_stock":
            stock_alert_counter.add(1, {"alert_type": "low_stock", "product_name": product.name})
            logger.warning('LOW STOCK ALERT: %s dropped to %d units',
                           product.name, product.stock,
                           extra={"custom_dimensions": {
                               "product_id": product_id, "product_name": product.name,
                               "remaining_stock": product.stock, "alert_type": "low_stock"
                           }})
        elif product.status == "out_of_stock":
            stock_alert_counter.add(1, {"alert_type": "out_of_stock", "product_name": product.name})
            logger.error('OUT OF STOCK: %s has 0 units remaining',
                         product.name,
                         extra={"custom_dimensions": {
                             "product_id": product_id, "product_name": product.name,
                             "alert_type": "out_of_stock"
                         }})

        # 8. API LATENCY HISTOGRAM — measures endpoint response time
        elapsed_ms = (time.time() - start_time) * 1000
        api_latency_histogram.record(elapsed_ms, {"endpoint": "purchase", "status": "success"})

        logger.info('Purchase completed successfully',
                    extra={"custom_dimensions": {
                        "product_id": product_id, "product_name": product.name,
                        "category": product.category, "quantity": qty,
                        "revenue": revenue, "remaining_stock": product.stock,
                        "new_status": product.status,
                        "user_id": session.get("user_id", "unknown"),
                        "user_name": session.get("user_name", "unknown"),
                        "user_role": session.get("user_role", "unknown"),
                    }})
        return jsonify({"message": "Purchase successful", "product": product.to_dict(), "quantity_purchased": qty})


# ========== HEALTH ==========
@app.route('/api/health')
def health():
    try:
        Product.query.first()
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"
    return jsonify({"status": "healthy", "app": "sre-demo-flask", "database": db_status, "timestamp": time.time()})


# ========== USER CONTEXT ==========
@app.route('/api/whoami')
def whoami():
    """Show current user identity — useful for verifying user tracking."""
    return jsonify({
        "user_id": session.get("user_id", "unknown"),
        "user_name": session.get("user_name", "unknown"),
        "user_role": session.get("user_role", "unknown"),
        "session_id": session.get("session_id", "unknown"),
    })


# ========== SRE CHAOS ENDPOINTS ==========
def _check_chaos_allowed(endpoint_name: str):
    """Return a 403 response if chaos is disabled or the cooldown has not elapsed."""
    if not ENABLE_CHAOS_ENDPOINTS:
        return jsonify({"error": "Chaos endpoints are disabled", "hint": "Set ENABLE_CHAOS_ENDPOINTS=true to enable"}), 403
    now = time.time()
    with _chaos_lock:
        last = _chaos_last_called.get(endpoint_name, 0)
        if now - last < CHAOS_COOLDOWN_SECONDS:
            remaining = int(CHAOS_COOLDOWN_SECONDS - (now - last))
            return jsonify({"error": "Cooldown active", "retry_after_seconds": remaining}), 429
        _chaos_last_called[endpoint_name] = now
    return None


@app.route('/api/stress/cpu')
def stress_cpu():
    guard = _check_chaos_allowed("stress_cpu")
    if guard is not None:
        return guard
    seconds = min(int(request.args.get('seconds', 10)), 30)
    logger.warning('CPU stress test for %ds', seconds)
    end = time.time() + seconds
    while time.time() < end:
        _ = sum(i * i for i in range(1000))
    return jsonify({"message": "CPU stress completed", "duration_seconds": seconds})


@app.route('/api/simulate/incident')
def simulate_incident():
    guard = _check_chaos_allowed("simulate_incident")
    if guard is not None:
        return guard
    logger.critical('INCIDENT SIMULATION: generating error burst with DB pressure')
    errors = []
    for i in range(20):
        try:
            Product.query.filter(Product.name == f"nonexistent-{random.randint(0,999999)}").count()
            raise Exception(f"Cascading failure #{i}: redis connection refused")
        except Exception as e:
            logger.error('Cascading failure event %d: %s', i, str(e))
            errors.append(str(e))
        time.sleep(0.1)
    return jsonify({"message": "Incident simulation complete", "error_count": len(errors)})


@app.errorhandler(Exception)
def handle_exception(e):
    logger.error('Unhandled exception: %s', str(e), exc_info=True)
    return jsonify({"error": str(e), "type": type(e).__name__}), 500


if __name__ == '__main__':
    app.run()

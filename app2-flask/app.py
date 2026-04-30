import os
import time
import random
import logging
import uuid
import json

from flask import Flask, redirect, render_template, request, send_from_directory, url_for, jsonify, session
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace, metrics
import requests as http_requests

# Configure Azure Monitor OpenTelemetry
configure_azure_monitor()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== AZURE SERVICE CLIENTS (auto-traced by OpenTelemetry) ==========
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
from azure.keyvault.secrets import SecretClient

_credential = None
_blob_client = None
_kv_client = None

def get_credential():
    global _credential
    if not _credential:
        try:
            _credential = DefaultAzureCredential()
        except Exception:
            _credential = None
    return _credential

def get_blob_client():
    global _blob_client
    if not _blob_client:
        storage_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
        cred = get_credential()
        if storage_url and cred:
            try:
                _blob_client = BlobServiceClient(storage_url, credential=cred)
            except Exception as e:
                logger.warning("Failed to init blob client: %s", e)
    return _blob_client

def get_kv_client():
    global _kv_client
    if not _kv_client:
        kv_url = os.environ.get("AZURE_KEYVAULT_URL")
        cred = get_credential()
        if kv_url and cred:
            try:
                _kv_client = SecretClient(vault_url=kv_url, credential=cred)
            except Exception as e:
                logger.warning("Failed to init Key Vault client: %s", e)
    return _kv_client

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


# ========== CURRENCY CONVERSION (live external API call) ==========
@app.route('/api/products/<int:product_id>/price')
def get_product_price(product_id):
    """Get product price converted to any currency using live exchange rates.
    Query params: currency (default: USD), quantity (default: 1)
    Each call hits the external exchange rate API — auto-traced as HTTP dependency."""
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404

    currency = request.args.get('currency', 'USD').upper()
    qty = int(request.args.get('quantity', 1))
    base_total = round(product.price * qty, 2)

    if currency == 'USD':
        return jsonify({
            "product": product.name, "quantity": qty,
            "currency": "USD", "unit_price": product.price,
            "total": base_total, "exchange_rate": 1.0
        })

    # Call external exchange rate API (auto-traced by OpenTelemetry)
    ext_api_url = os.environ.get("EXTERNAL_API_URL", "https://open.er-api.com/v6/latest/USD")
    try:
        resp = http_requests.get(ext_api_url, timeout=5)
        resp.raise_for_status()
        rates = resp.json().get("rates", {})
        rate = rates.get(currency)
        if not rate:
            return jsonify({"error": f"Currency '{currency}' not supported", "available": sorted(rates.keys())}), 400

        converted_price = round(product.price * rate, 2)
        converted_total = round(base_total * rate, 2)
        logger.info("Currency conversion: %s x%d = %.2f USD -> %.2f %s (rate: %.4f)",
                     product.name, qty, base_total, converted_total, currency, rate,
                     extra={"custom_dimensions": {
                         "product_id": product_id, "product_name": product.name,
                         "currency": currency, "exchange_rate": rate,
                         "usd_amount": base_total, "converted_amount": converted_total
                     }})
        return jsonify({
            "product": product.name, "quantity": qty,
            "currency": currency, "unit_price_usd": product.price,
            "unit_price_converted": converted_price,
            "total_usd": base_total, "total_converted": converted_total,
            "exchange_rate": rate
        })
    except http_requests.Timeout:
        logger.error("Exchange rate API timeout for currency %s", currency)
        return jsonify({"error": "Exchange rate service timeout", "currency": currency}), 504
    except Exception as e:
        logger.error("Exchange rate API error: %s", str(e))
        return jsonify({"error": f"Exchange rate service error: {str(e)}"}), 502


@app.route('/api/currencies')
def list_currencies():
    """List all available currencies from the exchange rate API."""
    ext_api_url = os.environ.get("EXTERNAL_API_URL", "https://open.er-api.com/v6/latest/USD")
    try:
        resp = http_requests.get(ext_api_url, timeout=5)
        rates = resp.json().get("rates", {})
        return jsonify({"base": "USD", "currencies": sorted(rates.keys()), "count": len(rates)})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


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

    # CHAOS_MODE check — persistent failure that requires config fix to resolve
    chaos_mode = os.environ.get("CHAOS_MODE", "").lower()
    if chaos_mode == "persistent":
        logger.error('Purchase blocked: CHAOS_MODE=persistent is enabled',
                     extra={"custom_dimensions": {
                         "product_id": product_id,
                         "failure_reason": "chaos_mode_persistent",
                         "resolution": "Remove CHAOS_MODE app setting to restore service"
                     }})
        return jsonify({"error": "Service degraded: purchase processing is disabled", "chaos_mode": True}), 503

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

        roll = random.randint(1, 10)
        if roll <= 2:
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

        if roll == 3:
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

        # ========== DOWNSTREAM DEPENDENCY CALLS (auto-traced) ==========
        # 9. AZURE BLOB STORAGE — upload purchase receipt
        try:
            blob_svc = get_blob_client()
            if blob_svc:
                receipt = json.dumps({
                    "purchase_id": str(uuid.uuid4()),
                    "product_id": product_id, "product_name": product.name,
                    "quantity": qty, "revenue": revenue,
                    "user_id": session.get("user_id", "unknown"),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                })
                blob_name = f"receipts/{time.strftime('%Y/%m/%d')}/{uuid.uuid4()}.json"
                container = blob_svc.get_container_client("purchase-receipts")
                container.upload_blob(name=blob_name, data=receipt, overwrite=True)
                span.set_attribute("receipt.blob_path", blob_name)
                logger.info("Receipt uploaded to blob: %s", blob_name)
        except Exception as e:
            logger.warning("Failed to upload receipt to blob: %s", str(e))

        # 10. EXTERNAL HTTP API — fetch exchange rate
        try:
            ext_api_url = os.environ.get("EXTERNAL_API_URL", "https://open.er-api.com/v6/latest/USD")
            ext_resp = http_requests.get(ext_api_url, timeout=5)
            if ext_resp.status_code == 200:
                rates = ext_resp.json().get("rates", {})
                span.set_attribute("exchange_rate.EUR", rates.get("EUR", 0))
                logger.info("Exchange rate fetched: EUR=%s", rates.get("EUR"))
        except Exception as e:
            logger.warning("Failed to fetch exchange rate: %s", str(e))

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


# ========== DEPENDENCY STATUS ==========
@app.route('/api/dependencies/status')
def dependency_status():
    """Check all downstream dependencies — each call is auto-traced by OpenTelemetry."""
    results = {}

    # 1. Database
    try:
        Product.query.first()
        results["postgresql"] = {"status": "connected", "target": "sre-demo-pg.postgres.database.azure.com"}
    except Exception as e:
        results["postgresql"] = {"status": "error", "error": str(e)}

    # 2. Azure Blob Storage
    try:
        blob_svc = get_blob_client()
        if blob_svc:
            container = blob_svc.get_container_client("purchase-receipts")
            props = container.get_container_properties()
            results["blob_storage"] = {"status": "connected", "target": os.environ.get("AZURE_STORAGE_ACCOUNT_URL", ""), "container": "purchase-receipts"}
        else:
            results["blob_storage"] = {"status": "not_configured"}
    except Exception as e:
        results["blob_storage"] = {"status": "error", "error": str(e)}

    # 3. Azure Key Vault
    try:
        kv = get_kv_client()
        if kv:
            secret = kv.get_secret("api-key")
            results["key_vault"] = {"status": "connected", "target": os.environ.get("AZURE_KEYVAULT_URL", ""), "secret_retrieved": True}
        else:
            results["key_vault"] = {"status": "not_configured"}
    except Exception as e:
        results["key_vault"] = {"status": "error", "error": str(e)}

    # 4. External HTTP API
    try:
        ext_url = os.environ.get("EXTERNAL_API_URL", "https://open.er-api.com/v6/latest/USD")
        resp = http_requests.get(ext_url, timeout=5)
        results["external_api"] = {"status": "connected" if resp.status_code == 200 else "error", "target": ext_url, "status_code": resp.status_code}
    except Exception as e:
        results["external_api"] = {"status": "error", "error": str(e)}

    all_ok = all(d.get("status") in ("connected", "not_configured") for d in results.values())
    return jsonify({"overall": "healthy" if all_ok else "degraded", "dependencies": results})


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
@app.route('/api/stress/cpu')
def stress_cpu():
    seconds = min(int(request.args.get('seconds', 10)), 30)
    logger.warning('CPU stress test for %ds', seconds)
    end = time.time() + seconds
    while time.time() < end:
        _ = sum(i * i for i in range(1000))
    return jsonify({"message": "CPU stress completed", "duration_seconds": seconds})


@app.route('/api/simulate/incident')
def simulate_incident():
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


# ========== PERSISTENT FAILURE SCENARIOS ==========
# These require config changes to resolve — not transient

_memory_hog = []  # holds allocated memory blocks

@app.route('/api/chaos/memory', methods=['POST'])
def chaos_memory_pressure():
    """Allocate memory that won't be freed — causes OOM over time.
    Resolution: restart the app or call DELETE /api/chaos/memory"""
    mb = int(request.args.get('mb', 50))
    mb = min(mb, 200)  # cap at 200MB per call
    _memory_hog.append(bytearray(mb * 1024 * 1024))
    total_mb = sum(len(b) for b in _memory_hog) // (1024 * 1024)
    logger.critical('MEMORY PRESSURE: allocated %dMB, total held: %dMB',
                    mb, total_mb,
                    extra={"custom_dimensions": {
                        "failure_reason": "memory_pressure",
                        "allocated_mb": mb,
                        "total_held_mb": total_mb,
                        "resolution": "Restart app service or call DELETE /api/chaos/memory"
                    }})
    return jsonify({"message": f"Allocated {mb}MB", "total_held_mb": total_mb}), 200


@app.route('/api/chaos/memory', methods=['DELETE'])
def chaos_memory_release():
    """Release all held memory."""
    released = sum(len(b) for b in _memory_hog) // (1024 * 1024)
    _memory_hog.clear()
    logger.info('Memory released: %dMB freed', released)
    return jsonify({"message": f"Released {released}MB"})


@app.route('/api/chaos/status')
def chaos_status():
    """Check current chaos state."""
    chaos_mode = os.environ.get("CHAOS_MODE", "disabled")
    memory_held = sum(len(b) for b in _memory_hog) // (1024 * 1024)
    return jsonify({
        "chaos_mode": chaos_mode,
        "memory_held_mb": memory_held,
        "db_healthy": _check_db(),
    })


def _check_db():
    try:
        Product.query.first()
        return True
    except:
        return False


@app.errorhandler(Exception)
def handle_exception(e):
    logger.error('Unhandled exception: %s', str(e), exc_info=True)
    return jsonify({"error": str(e), "type": type(e).__name__}), 500


if __name__ == '__main__':
    app.run()

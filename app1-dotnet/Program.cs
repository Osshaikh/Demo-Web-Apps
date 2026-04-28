using System.Diagnostics;
using System.Text.Json;
using Microsoft.EntityFrameworkCore;
using Azure.Monitor.OpenTelemetry.AspNetCore;
using Azure.Storage.Blobs;
using Azure.Security.KeyVault.Secrets;
using Azure.Core;

var builder = WebApplication.CreateBuilder(args);

// Azure Monitor OpenTelemetry — sends traces, metrics, logs to Application Insights
builder.Services.AddOpenTelemetry().UseAzureMonitor();
builder.Services.AddHttpClient();

// Azure service clients (auto-traced by OpenTelemetry)
// Registered as optional — app still works if Azure services unavailable
TokenCredential credential = new Azure.Identity.ManagedIdentityCredential();
var storageUrl = builder.Configuration["AZURE_STORAGE_ACCOUNT_URL"];
var kvUrl = builder.Configuration["AZURE_KEYVAULT_URL"];

try
{
    if (!string.IsNullOrEmpty(storageUrl))
        builder.Services.AddSingleton(new BlobServiceClient(new Uri(storageUrl), credential));
    if (!string.IsNullOrEmpty(kvUrl))
        builder.Services.AddSingleton(new SecretClient(new Uri(kvUrl), credential));
}
catch { /* Azure clients will be null if init fails — handled gracefully in endpoints */ }

// Entity Framework Core with SQL Server
var connStr = builder.Configuration.GetConnectionString("SqlDb")
    ?? builder.Configuration["SQL_CONNECTION_STRING"]
    ?? "Server=sre-demo-sql.database.windows.net;Database=sre-orders-db;User Id=sreadmin;Password=SreDemo2026!956;Encrypt=True;TrustServerCertificate=False;";
builder.Services.AddDbContext<OrderDbContext>(options => options.UseSqlServer(connStr));

var app = builder.Build();

var logger = app.Services.GetRequiredService<ILoggerFactory>().CreateLogger("SREDemoApp");

// Auto-create tables on startup
using (var scope = app.Services.CreateScope())
{
    var db = scope.ServiceProvider.GetRequiredService<OrderDbContext>();
    db.Database.EnsureCreated();
    if (!db.Orders.Any())
    {
        db.Orders.AddRange(
            new Order { CustomerName = "Acme Corp", Product = "Cloud License", Amount = 4999.99m, Status = "Completed", CreatedAt = DateTime.UtcNow.AddDays(-5) },
            new Order { CustomerName = "Globex Inc", Product = "Support Plan", Amount = 1200.00m, Status = "Pending", CreatedAt = DateTime.UtcNow.AddDays(-3) },
            new Order { CustomerName = "Initech", Product = "API Gateway", Amount = 8500.00m, Status = "Completed", CreatedAt = DateTime.UtcNow.AddDays(-1) },
            new Order { CustomerName = "Umbrella LLC", Product = "Data Storage", Amount = 3200.00m, Status = "Processing", CreatedAt = DateTime.UtcNow },
            new Order { CustomerName = "Stark Industries", Product = "AI Compute", Amount = 15000.00m, Status = "Completed", CreatedAt = DateTime.UtcNow }
        );
        db.SaveChanges();
        logger.LogInformation("Seeded {Count} orders into database", 5);
    }
}

// ========== STATIC FILES & HEALTH ==========
app.UseStaticFiles();
app.MapGet("/", () => Results.File("index.html", "text/html"));
app.MapGet("/api/health", () => Results.Ok(new { status = "healthy", app = "sre-demo-dotnet", service = "Order Management API", timestamp = DateTime.UtcNow }));
app.MapGet("/health", () => Results.Ok(new { status = "healthy" }));

// ========== DEPENDENCY STATUS (each call is auto-traced) ==========
app.MapGet("/api/dependencies/status", async (OrderDbContext db) =>
{
    var results = new Dictionary<string, object>();

    // 1. SQL Server
    try { await db.Orders.FirstOrDefaultAsync(); results["sql_server"] = new { status = "connected", target = "sre-demo-sql.database.windows.net" }; }
    catch (Exception ex) { results["sql_server"] = new { status = "error", error = ex.Message }; }

    // 2. Blob Storage
    try
    {
        var blobSvc = app.Services.GetService<BlobServiceClient>();
        if (blobSvc != null)
        {
            var container = blobSvc.GetBlobContainerClient("order-confirmations");
            await container.GetPropertiesAsync();
            results["blob_storage"] = new { status = "connected", target = app.Configuration["AZURE_STORAGE_ACCOUNT_URL"] ?? "" };
        }
        else results["blob_storage"] = new { status = "not_configured" };
    }
    catch (Exception ex) { results["blob_storage"] = new { status = "error", error = ex.Message }; }

    // 3. Key Vault
    try
    {
        var kvClient = app.Services.GetService<SecretClient>();
        if (kvClient != null)
        {
            await kvClient.GetSecretAsync("api-key");
            results["key_vault"] = new { status = "connected", target = app.Configuration["AZURE_KEYVAULT_URL"] ?? "" };
        }
        else results["key_vault"] = new { status = "not_configured" };
    }
    catch (Exception ex) { results["key_vault"] = new { status = "error", error = ex.Message }; }

    // 4. External HTTP API
    try
    {
        var httpFactory = app.Services.GetRequiredService<IHttpClientFactory>();
        var client = httpFactory.CreateClient();
        var extUrl = app.Configuration["EXTERNAL_API_URL"] ?? "https://open.er-api.com/v6/latest/USD";
        var resp = await client.GetAsync(extUrl);
        results["external_api"] = new { status = resp.IsSuccessStatusCode ? "connected" : "error", target = extUrl, statusCode = (int)resp.StatusCode };
    }
    catch (Exception ex) { results["external_api"] = new { status = "error", error = ex.Message }; }

    var allOk = results.Values.All(v => v.GetType().GetProperty("status")?.GetValue(v)?.ToString() is "connected" or "not_configured");
    return Results.Ok(new { overall = allOk ? "healthy" : "degraded", dependencies = results });
});

// ========== CRUD: LIST ORDERS ==========
app.MapGet("/api/orders", async (OrderDbContext db) =>
{
    logger.LogInformation("Fetching all orders from database");
    var orders = await db.Orders.OrderByDescending(o => o.CreatedAt).ToListAsync();
    return Results.Ok(orders);
});

// ========== CRUD: GET ORDER BY ID ==========
app.MapGet("/api/orders/{id}", async (int id, OrderDbContext db) =>
{
    logger.LogInformation("Fetching order {OrderId}", id);
    var order = await db.Orders.FindAsync(id);
    if (order is null)
    {
        logger.LogWarning("Order {OrderId} not found", id);
        return Results.NotFound(new { error = "Order not found", orderId = id });
    }
    return Results.Ok(order);
});

// ========== CRUD: CREATE ORDER ==========
app.MapPost("/api/orders", async (CreateOrderRequest req, OrderDbContext db) =>
{
    logger.LogInformation("Creating order for customer {Customer}, product {Product}", req.CustomerName, req.Product);
    var order = new Order
    {
        CustomerName = req.CustomerName,
        Product = req.Product,
        Amount = req.Amount,
        Status = "Pending",
        CreatedAt = DateTime.UtcNow
    };
    db.Orders.Add(order);
    await db.SaveChangesAsync();
    logger.LogInformation("Order {OrderId} created successfully", order.Id);
    return Results.Created($"/api/orders/{order.Id}", order);
});

// ========== CRUD: UPDATE ORDER ==========
app.MapPut("/api/orders/{id}", async (int id, UpdateOrderRequest req, OrderDbContext db) =>
{
    var order = await db.Orders.FindAsync(id);
    if (order is null) return Results.NotFound(new { error = "Order not found" });

    order.Status = req.Status ?? order.Status;
    order.Amount = req.Amount ?? order.Amount;
    await db.SaveChangesAsync();
    logger.LogInformation("Order {OrderId} updated: status={Status}", id, order.Status);
    return Results.Ok(order);
});

// ========== CRUD: DELETE ORDER ==========
app.MapDelete("/api/orders/{id}", async (int id, OrderDbContext db) =>
{
    var order = await db.Orders.FindAsync(id);
    if (order is null) return Results.NotFound(new { error = "Order not found" });

    db.Orders.Remove(order);
    await db.SaveChangesAsync();
    logger.LogWarning("Order {OrderId} deleted", id);
    return Results.Ok(new { message = "Deleted", orderId = id });
});

// ========== SEARCH / AGGREGATE ==========
app.MapGet("/api/orders/search", async (string? customer, string? status, OrderDbContext db) =>
{
    logger.LogInformation("Searching orders: customer={Customer}, status={Status}", customer, status);
    var query = db.Orders.AsQueryable();
    if (!string.IsNullOrEmpty(customer)) query = query.Where(o => o.CustomerName.Contains(customer));
    if (!string.IsNullOrEmpty(status)) query = query.Where(o => o.Status == status);
    return Results.Ok(await query.ToListAsync());
});

app.MapGet("/api/orders/stats", async (OrderDbContext db) =>
{
    logger.LogInformation("Computing order statistics");
    var stats = new
    {
        TotalOrders = await db.Orders.CountAsync(),
        TotalRevenue = await db.Orders.SumAsync(o => o.Amount),
        ByStatus = await db.Orders.GroupBy(o => o.Status)
            .Select(g => new { Status = g.Key, Count = g.Count(), Revenue = g.Sum(o => o.Amount) })
            .ToListAsync()
    };
    return Results.Ok(stats);
});

// ========== PAYMENT PROCESSING (error-prone) ==========
app.MapPost("/api/orders/{id}/pay", async (int id, OrderDbContext db) =>
{
    var order = await db.Orders.FindAsync(id);
    if (order is null) return Results.NotFound(new { error = "Order not found" });

    var roll = Random.Shared.Next(1, 10);
    if (roll <= 3)
    {
        logger.LogError("Payment gateway timeout for order {OrderId}", id);
        throw new TimeoutException($"Payment gateway timeout processing order {id}");
    }
    if (roll == 4)
    {
        logger.LogError("Payment declined for order {OrderId}: insufficient funds", id);
        throw new InvalidOperationException($"Payment declined for order {id}: insufficient funds");
    }

    order.Status = "Paid";
    await db.SaveChangesAsync();
    logger.LogInformation("Payment completed for order {OrderId}, amount {Amount}", id, order.Amount);

    // DOWNSTREAM: Upload order confirmation to Blob Storage (auto-traced)
    try
    {
        var blobSvc = app.Services.GetService<BlobServiceClient>();
        if (blobSvc != null)
        {
            var container = blobSvc.GetBlobContainerClient("order-confirmations");
            var confirmation = JsonSerializer.Serialize(new { orderId = id, amount = order.Amount, customer = order.CustomerName, paidAt = DateTime.UtcNow });
            var blobName = $"confirmations/{DateTime.UtcNow:yyyy/MM/dd}/{Guid.NewGuid()}.json";
            await container.UploadBlobAsync(blobName, new BinaryData(confirmation));
            logger.LogInformation("Order confirmation uploaded to blob: {BlobName}", blobName);
        }
    }
    catch (Exception ex) { logger.LogWarning(ex, "Failed to upload order confirmation to blob"); }

    // DOWNSTREAM: Call external notification API (auto-traced)
    try
    {
        var httpFactory = app.Services.GetRequiredService<IHttpClientFactory>();
        var client = httpFactory.CreateClient();
        var extUrl = app.Configuration["EXTERNAL_API_URL"] ?? "https://open.er-api.com/v6/latest/USD";
        var extResp = await client.GetAsync(extUrl);
        logger.LogInformation("External API called: status={StatusCode}", (int)extResp.StatusCode);
    }
    catch (Exception ex) { logger.LogWarning(ex, "Failed to call external API"); }

    return Results.Ok(new { message = "Payment successful", orderId = id, amount = order.Amount });
});

// ========== EXTERNAL DEPENDENCY ==========
app.MapGet("/api/external", async (IHttpClientFactory httpFactory) =>
{
    var client = httpFactory.CreateClient();
    logger.LogInformation("Calling external API");
    try
    {
        var response = await client.GetAsync("https://httpbin.org/delay/1");
        return Results.Ok(new { status = (int)response.StatusCode, source = "httpbin.org" });
    }
    catch (Exception ex)
    {
        logger.LogError(ex, "External API call failed");
        return Results.StatusCode(502);
    }
});

// ========== SRE CHAOS ENDPOINTS ==========
app.MapGet("/api/stress/cpu", (int? seconds) =>
{
    var duration = Math.Min(seconds ?? 10, 30);
    logger.LogWarning("CPU stress test started for {Duration}s", duration);
    var sw = Stopwatch.StartNew();
    while (sw.Elapsed.TotalSeconds < duration) { _ = Math.Sqrt(Random.Shared.NextDouble()); }
    return Results.Ok(new { message = "CPU stress completed", durationSeconds = duration });
});

app.MapGet("/api/stress/memory", (int? megabytes) =>
{
    var mb = Math.Min(megabytes ?? 50, 200);
    logger.LogWarning("Memory pressure test: allocating {MB}MB", mb);
    var data = new byte[mb * 1024 * 1024];
    Random.Shared.NextBytes(data);
    return Results.Ok(new { message = $"Allocated {mb}MB", checksum = data.Take(8).Sum(b => b) });
});

app.MapGet("/api/simulate/incident", async (OrderDbContext db) =>
{
    logger.LogCritical("INCIDENT SIMULATION: generating error burst with DB pressure");
    var errors = new List<string>();
    for (int i = 0; i < 20; i++)
    {
        try
        {
            // Hammer the DB with bad queries to generate dependency failures
            await db.Orders.Where(o => o.CustomerName == $"nonexistent-{Guid.NewGuid()}").CountAsync();
            throw new Exception($"Cascading failure #{i}: connection pool exhausted");
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Cascading failure event {Index}", i);
            errors.Add(ex.Message);
        }
        await Task.Delay(100);
    }
    return Results.Ok(new { message = "Incident simulation complete", errorCount = errors.Count });
});

app.Run();

// ========== MODELS ==========
public class Order
{
    public int Id { get; set; }
    public string CustomerName { get; set; } = "";
    public string Product { get; set; } = "";
    public decimal Amount { get; set; }
    public string Status { get; set; } = "Pending";
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
}

public record CreateOrderRequest(string CustomerName, string Product, decimal Amount);
public record UpdateOrderRequest(string? Status, decimal? Amount);

public class OrderDbContext : DbContext
{
    public OrderDbContext(DbContextOptions<OrderDbContext> options) : base(options) { }
    public DbSet<Order> Orders => Set<Order>();
}

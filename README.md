# Demo-Web-Apps

This repository contains two demo web applications used for SRE monitoring and observability.

## Applications

### app1-dotnet — .NET Order Management API
- **Stack:** ASP.NET Core
- **Purpose:** Order processing API with Application Insights integration
- **Container:** Dockerfile included
- **Key files:** `Program.cs`, `appsettings.json`

### app2-flask — Flask Inventory App
- **Stack:** Python / Flask
- **Purpose:** Inventory management web app with Azure deployment support
- **Container:** Dockerfile included
- **Infra:** Bicep templates for Azure deployment
- **Key files:** `app.py`, `requirements.txt`
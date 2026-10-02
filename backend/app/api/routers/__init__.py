"""Router registry (the auth router is mounted separately in ``app.api.main``)."""

from app.api.routers import dashboard, reports, settings, transactions, users, wallets

ROUTERS = [
    dashboard.router,
    users.router,
    transactions.router,
    wallets.router,
    reports.router,
    settings.router,
]

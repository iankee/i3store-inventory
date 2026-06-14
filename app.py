"""Sricreate Inventory System — FastAPI backend.

Routes:
  GET  /                        Dashboard (web)
  GET  /login                   Login page (web)
  POST /auth/login              Login API (web + json)
  GET  /auth/logout             Logout
  POST /auth/register           Register new user (owner only)

  GET  /api/products            List products (json)
  POST /api/products            Create product (json)
  GET  /api/products/{id}       Product detail (json)
  PUT  /api/products/{id}       Update product (json)

  POST /api/movements/stock-in  Add stock (json + optional file)
  POST /api/movements/stock-out Remove stock (json)

  GET  /api/movements           Movement history (json, query params)
  GET  /api/stats               Summary statistics (json)

  GET  /api/users               List users (owner only)
  POST /api/users               Create user (owner only)
  DELETE /api/users/{id}        Delete user (owner only)

  POST /api/webhook/telegram    Telegram bot webhook
"""

import os
import shutil
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from auth import (
    create_access_token,
    get_current_user,
    get_db,
    hash_password,
    require_permission,
    seed_admin,
    verify_password,
)
from config import PORT, UPLOAD_FOLDER
from models import (
    ALL_PERMISSIONS,
    has_permission,
    MovementSource,
    MovementType,
    Product,
    StockMovement,
    User,
    UserRole,
    init_db,
)

# ── App setup ────────────────────────────────────────────────────

app = FastAPI(title="Sricreate Inventory", version="1.0.0")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")
templates = Jinja2Templates(directory="templates")


# ── Startup ──────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()
    seed_admin()


# ═══════════════════════════════════════════════════════════════════
#  WEB PAGES
# ═══════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request, db: Session = Depends(get_db)):
    """Main dashboard — requires login."""
    token = request.cookies.get("inv_token")
    if not token:
        return RedirectResponse("/users", status_code=302)


    from auth import decode_token
    payload = decode_token(token)
    if not payload:
        return RedirectResponse("/users", status_code=302)


    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        return RedirectResponse("/users", status_code=302)


    # Stats
    total_products = db.query(func.count(Product.id)).scalar() or 0
    total_stock = db.query(func.sum(Product.current_stock)).scalar() or 0
    low_stock = db.query(func.count(Product.id)).filter(
        Product.current_stock <= Product.min_stock, Product.is_active == 1
    ).scalar() or 0

    # 7-day stats
    seven_days_ago = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    seven_days_ago = seven_days_ago - timedelta(days=6)
    week_in = db.query(func.coalesce(func.sum(StockMovement.quantity), 0)).filter(
        StockMovement.type == MovementType.STOCK_IN,
        StockMovement.created_at >= seven_days_ago
    ).scalar() or 0
    week_out = db.query(func.coalesce(func.abs(func.sum(StockMovement.quantity)), 0)).filter(
        StockMovement.type == MovementType.STOCK_OUT,
        StockMovement.created_at >= seven_days_ago
    ).scalar() or 0

    # Recent movements
    recent = (
        db.query(StockMovement)
        .order_by(desc(StockMovement.created_at))
        .limit(20)
        .all()
    )

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "stats": {
            "total_products": total_products,
            "total_stock": total_stock,
            "low_stock": low_stock,
            "week_in": week_in,
            "week_out": week_out,
        },
        "recent": recent,
    })


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/products", response_class=HTMLResponse)
async def products_page(
    request: Request,
    search: str = Query(""),
    category: str = Query(""),
    in_stock: str = Query(""),
    sort: str = Query("name"),
    page: int = Query(1),
    db: Session = Depends(get_db),
):
    token = request.cookies.get("inv_token")
    from auth import decode_token
    payload = decode_token(token)
    if not payload:
        return RedirectResponse("/users", status_code=302)


    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        return RedirectResponse("/users", status_code=302)


    query = db.query(Product)

    # Search
    if search:
        query = query.filter(Product.name.ilike(f"%{search}%"))

    # Category filter (substring match on name)
    if category:
        query = query.filter(Product.name.ilike(f"%{category}%"))

    # In-stock only
    if in_stock == "1":
        query = query.filter(Product.current_stock > 0)

    # Sorting
    sort_map = {
        "name": Product.name,
        "stock_asc": Product.current_stock,
        "stock_desc": Product.current_stock.desc(),
        "newest": Product.created_at.desc(),
    }
    if sort in sort_map:
        query = query.order_by(sort_map[sort])
    elif sort == "recent_sold":
        # Products ordered by most recent stock_out movement
        from sqlalchemy import case
        latest_out = (
            db.query(
                StockMovement.product_id,
                func.max(StockMovement.created_at).label("last_out")
            )
            .filter(StockMovement.type == MovementType.STOCK_OUT)
            .group_by(StockMovement.product_id)
            .subquery()
        )
        query = query.outerjoin(
            latest_out, Product.id == latest_out.c.product_id
        ).order_by(
            case((latest_out.c.last_out is None, 1), else_=0),
            latest_out.c.last_out.desc(),
            Product.name,
        )
    else:
        query = query.order_by(Product.name)

    # Total for pagination
    total = query.count()

    # Extract categories for filter chips (unique first-word brands)
    all_names = db.query(Product.name).all()
    brand_counts = {}
    for (name,) in all_names:
        brand = name.split()[0]
        brand_counts[brand] = brand_counts.get(brand, 0) + 1
    categories = sorted(
        [{"name": b, "count": c} for b, c in brand_counts.items() if c >= 2],
        key=lambda x: x["count"], reverse=True
    )[:12]

    # Pagination
    per_page = 20
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    products = query.offset(offset).limit(per_page).all()

    return templates.TemplateResponse("products.html", {
        "request": request,
        "user": user,
        "products": products,
        "search": search,
        "category": category,
        "in_stock": in_stock,
        "sort": sort,
        "page": page,
        "total_pages": total_pages,
        "total_products": total,
        "per_page": per_page,
        "categories": categories,
    })


@app.post("/products/add", response_class=HTMLResponse)
async def add_product_web(
    request: Request,
    db: Session = Depends(get_db),
):
    """Add new product via web form."""
    token = request.cookies.get("inv_token")
    from auth import decode_token
    payload = decode_token(token)
    if not payload:
        return RedirectResponse("/users", status_code=302)


    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        return RedirectResponse("/users", status_code=302)

    if not has_permission(user, "products.create"):
        return RedirectResponse("/", status_code=302)

    if not has_permission(user, "products.create"):
        return RedirectResponse("/", status_code=302)


    form = await request.form()
    name = form.get("name", "").strip()
    sku = form.get("sku", "").strip() or None
    description = form.get("description", "").strip() or None
    min_stock = int(form.get("min_stock", 5))
    price_buy = form.get("price_buy", "").strip()
    price_sell = form.get("price_sell", "").strip()

    if not name:
        products = db.query(Product).order_by(Product.name).all()
        return templates.TemplateResponse("products.html", {
            "request": request,
            "user": user,
            "products": products,
            "search": "",
            "error": "Nama produk wajib diisi",
        })

    # Check SKU uniqueness
    if sku and db.query(Product).filter(Product.sku == sku).first():
        products = db.query(Product).order_by(Product.name).all()
        return templates.TemplateResponse("products.html", {
            "request": request,
            "user": user,
            "products": products,
            "search": "",
            "error": f"SKU '{sku}' sudah dipakai",
        })

    product = Product(
        name=name,
        sku=sku,
        description=description,
        min_stock=min_stock,
        price_buy=float(price_buy) if price_buy else None,
        price_sell=float(price_sell) if price_sell else None,
    )
    db.add(product)
    db.commit()
    db.refresh(product)

    return RedirectResponse("/users", status_code=302)



@app.get("/products/{product_id}", response_class=HTMLResponse)
async def product_detail_page(
    request: Request,
    product_id: int,
    db: Session = Depends(get_db),
):
    token = request.cookies.get("inv_token")
    from auth import decode_token
    payload = decode_token(token)
    if not payload:
        return RedirectResponse("/users", status_code=302)


    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        return RedirectResponse("/users", status_code=302)

    if not has_permission(user, "products.view"):
        return RedirectResponse("/", status_code=302)

    if not has_permission(user, "products.view"):
        return RedirectResponse("/", status_code=302)


    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    movements = (
        db.query(StockMovement)
        .filter(StockMovement.product_id == product_id)
        .order_by(desc(StockMovement.created_at))
        .all()
    )

    return templates.TemplateResponse("product_detail.html", {
        "request": request,
        "user": user,
        "product": product,
        "movements": movements,
    })


@app.post("/products/{product_id}/delete", response_class=HTMLResponse)
async def delete_product_web(
    request: Request,
    product_id: int,
    db: Session = Depends(get_db),
):
    """Delete product + its stock movements."""
    token = request.cookies.get("inv_token")
    from auth import decode_token
    payload = decode_token(token)
    if not payload:
        return RedirectResponse("/users", status_code=302)


    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        return RedirectResponse("/users", status_code=302)

    if not has_permission(user, "products.delete"):
        return RedirectResponse("/", status_code=302)

    if not has_permission(user, "products.delete"):
        return RedirectResponse("/", status_code=302)


    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Delete associated stock movements first (FK constraint)
    db.query(StockMovement).filter(StockMovement.product_id == product_id).delete()
    db.delete(product)
    db.commit()

    return RedirectResponse("/users", status_code=302)



@app.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    db: Session = Depends(get_db),
):
    token = request.cookies.get("inv_token")
    from auth import decode_token
    payload = decode_token(token)
    if not payload:
        return RedirectResponse("/users", status_code=302)


    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user or not has_permission(user, "users.manage"):
        return RedirectResponse("/users", status_code=302)


    users = db.query(User).order_by(User.created_at).all()
    return templates.TemplateResponse("users.html", {
        "request": request,
        "user": user,
        "users": users,
        "UserRole": UserRole,
        "ALL_PERMISSIONS": ALL_PERMISSIONS,
    })


# ═══════════════════════════════════════════════════════════════════
#  AUTH API
# ═══════════════════════════════════════════════════════════════════

@app.post("/auth/login")
async def login(
    request: Request,
    db: Session = Depends(get_db),
):
    """Login — accepts JSON or form data."""
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")
    else:
        form = await request.form()
        username = form.get("username", "")
        password = form.get("password", "")

    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        # Redirect back to login for web
        if "application/json" not in content_type:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Username atau password salah",
            })
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user.is_active:
        if "application/json" not in content_type:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Akun dinonaktifkan",
            })
        raise HTTPException(status_code=403, detail="Account disabled")

    token = create_access_token({"sub": str(user.id), "role": user.role.value})

    if "application/json" in content_type:
        return {"access_token": token, "token_type": "bearer"}

    response = RedirectResponse("/", status_code=302)
    response.set_cookie("inv_token", token, httponly=True, max_age=28800, samesite="lax")
    return response


@app.get("/auth/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("inv_token")
    return response


@app.post("/auth/register")
async def register(
    request: Request,
    db: Session = Depends(get_db),
    _owner: User = Depends(require_permission("users.manage")),
):
    """Register a new user (owner only)."""
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "").strip()
    display_name = form.get("display_name", "").strip()
    telegram_username = form.get("telegram_username", "").strip().lstrip("@")
    role = form.get("role", "admin")
    # Collect permissions from checkboxes (keys prefixed with "perm_")
    import json as _json
    perm_keys = [k[5:] for k in form.keys() if k.startswith("perm_")]
    perms_json = _json.dumps(perm_keys) if perm_keys else None

    if not username or not password or not display_name:
        users = db.query(User).order_by(User.created_at).all()
        return templates.TemplateResponse("users.html", {
            "request": request,
            "user": _owner,
            "users": users,
            "UserRole": UserRole,
            "ALL_PERMISSIONS": ALL_PERMISSIONS,
            "error": "Semua field wajib diisi",
        })

    if db.query(User).filter(User.username == username).first():
        users = db.query(User).order_by(User.created_at).all()
        return templates.TemplateResponse("users.html", {
            "request": request,
            "user": _owner,
            "users": users,
            "UserRole": UserRole,
            "ALL_PERMISSIONS": ALL_PERMISSIONS,
            "error": "Username sudah dipakai",
        })

    new_user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name,
        role=UserRole(role) if role in [r.value for r in UserRole] else UserRole.VIEWER,
        telegram_username=telegram_username or None,
        permissions=perms_json,
    )
    db.add(new_user)
    db.commit()

    return RedirectResponse("/users", status_code=302)




@app.post("/api/users/{user_id}/permissions")
async def update_user_permissions(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    """Update user permissions (users.manage required)."""
    if current_user.id == user_id:
        raise HTTPException(status_code=400, detail="Cannot modify your own permissions")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404)
    form = await request.form()
    import json as _json
    perm_keys = [k[5:] for k in form.keys() if k.startswith("perm_")]
    user.permissions = _json.dumps(perm_keys) if perm_keys else None
    db.commit()
    return RedirectResponse("/users", status_code=302)

@app.post("/api/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    if current_user.id == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404)
    db.delete(user)
    db.commit()
    return RedirectResponse("/users", status_code=302)



@app.post("/api/users/{user_id}/toggle")
async def toggle_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("users.manage")),
):
    if current_user.id == user_id:
        raise HTTPException(status_code=400, detail="Cannot disable yourself")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404)
    user.is_active = 0 if user.is_active else 1
    db.commit()
    return RedirectResponse("/users", status_code=302)



# ═══════════════════════════════════════════════════════════════════
#  PRODUCTS API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/products")
async def api_products(
    search: str = Query(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not has_permission(current_user, "products.view"):
        raise HTTPException(status_code=403, detail="Permission denied")

    query = db.query(Product)
    if search:
        query = query.filter(Product.name.ilike(f"%{search}%"))
    products = query.order_by(Product.name).all()
    return {"products": [p.__dict__ for p in products]}


@app.post("/api/products")
async def api_create_product(
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    if not has_permission(_user, "products.create"):
        raise HTTPException(status_code=403, detail="Permission denied")
    data = await request.json()
    product = Product(
        name=data["name"],
        sku=data.get("sku"),
        description=data.get("description"),
        min_stock=data.get("min_stock", 5),
        price_buy=data.get("price_buy"),
        price_sell=data.get("price_sell"),
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return {"product": product.__dict__}


@app.put("/api/products/{product_id}")
async def api_update_product(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    if not has_permission(_user, "products.edit"):
        raise HTTPException(status_code=403, detail="Permission denied")

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404)

    data = await request.json()
    for field in ["name", "sku", "description", "min_stock", "price_buy", "price_sell"]:
        if field in data:
            setattr(product, field, data[field])

    db.commit()
    db.refresh(product)
    return {"product": product.__dict__}


# ═══════════════════════════════════════════════════════════════════
#  STOCK MOVEMENT API
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/movements/stock-in")
async def api_stock_in(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add stock via web form or JSON."""
    if not has_permission(current_user, "stock.in"):
        raise HTTPException(status_code=403, detail="Permission denied")

    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        product_id = int(form.get("product_id"))
        quantity = int(form.get("quantity"))
        notes = form.get("notes", "")
        source = form.get("source", MovementSource.WEB.value)
        photo: Optional[UploadFile] = form.get("photo")
    else:
        data = await request.json()
        product_id = data["product_id"]
        quantity = data["quantity"]
        notes = data.get("notes", "")
        source = data.get("source", MovementSource.WEB.value)
        photo = None

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Save photo if uploaded
    photo_path = None
    if photo and photo.filename:
        filename = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{photo.filename}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        with open(filepath, "wb") as f:
            content = await photo.read()
            f.write(content)
        photo_path = filename

    before = product.current_stock
    product.current_stock += quantity

    movement = StockMovement(
        product_id=product.id,
        user_id=current_user.id if current_user else None,
        type=MovementType.STOCK_IN,
        source=MovementSource(source) if source in [s.value for s in MovementSource] else MovementSource.WEB,
        quantity=quantity,
        stock_before=before,
        stock_after=product.current_stock,
        notes=notes,
        photo_path=photo_path,
    )
    db.add(movement)
    db.commit()

    # Redirect for web form
    if "multipart/form-data" in content_type:
        return RedirectResponse("/users", status_code=302)


    return {
        "ok": True,
        "product": product.name,
        "quantity": quantity,
        "stock_before": before,
        "stock_after": product.current_stock,
    }


@app.post("/api/movements/stock-out")
async def api_stock_out(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not has_permission(current_user, "stock.out"):
        raise HTTPException(status_code=403, detail="Permission denied")
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        product_id = int(form.get("product_id"))
        quantity = int(form.get("quantity"))
        notes = form.get("notes", "")
        source = form.get("source", MovementSource.WEB.value)
    else:
        data = await request.json()
        product_id = data["product_id"]
        quantity = data["quantity"]
        notes = data.get("notes", "")
        source = data.get("source", MovementSource.WEB.value)

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    if product.current_stock < quantity:
        if "multipart/form-data" in content_type:
            return JSONResponse({"error": "Stok tidak mencukupi"}, status_code=400)
        raise HTTPException(status_code=400, detail="Insufficient stock")

    before = product.current_stock
    product.current_stock -= quantity

    movement = StockMovement(
        product_id=product.id,
        user_id=current_user.id,
        type=MovementType.STOCK_OUT,
        source=MovementSource(source) if source in [s.value for s in MovementSource] else MovementSource.WEB,
        quantity=-quantity,
        stock_before=before,
        stock_after=product.current_stock,
        notes=notes,
    )
    db.add(movement)
    db.commit()

    if "multipart/form-data" in content_type:
        return RedirectResponse("/users", status_code=302)


    return {
        "ok": True,
        "product": product.name,
        "quantity": -quantity,
        "stock_before": before,
        "stock_after": product.current_stock,
    }


@app.get("/api/movements")
async def api_movements(
    product_id: Optional[int] = Query(None),
    type: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not has_permission(current_user, "products.view"):
        raise HTTPException(status_code=403, detail="Permission denied")

    query = db.query(StockMovement)
    if product_id:
        query = query.filter(StockMovement.product_id == product_id)
    if type:
        query = query.filter(StockMovement.type == type)
    movements = query.order_by(desc(StockMovement.created_at)).limit(limit).all()

    results = []
    for m in movements:
        results.append({
            "id": m.id,
            "product_name": m.product.name if m.product else "?",
            "type": m.type.value,
            "source": m.source.value,
            "quantity": m.quantity,
            "stock_before": m.stock_before,
            "stock_after": m.stock_after,
            "notes": m.notes,
            "photo_url": f"/uploads/{m.photo_path}" if m.photo_path else None,
            "created_at": m.created_at.isoformat(),
        })

    return {"movements": results}


@app.get("/api/stats")
async def api_stats(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not has_permission(current_user, "reports.view"):
        raise HTTPException(status_code=403, detail="Permission denied")

    return {
        "total_products": db.query(func.count(Product.id)).scalar() or 0,
        "total_stock": db.query(func.sum(Product.current_stock)).scalar() or 0,
        "low_stock": db.query(func.count(Product.id))
        .filter(Product.current_stock <= Product.min_stock, Product.is_active == 1)
        .scalar() or 0,
    }


@app.get("/api/stats/trend")
async def api_trend(db: Session = Depends(get_db)):
    """7-day trend data for chart."""
    from datetime import timedelta
    today = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59)
    days = []
    labels = []
    in_data = []
    out_data = []

    for i in range(6, -1, -1):
        day_start = (today - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        labels.append(day_start.strftime("%d/%m"))

        day_in = db.query(func.coalesce(func.sum(StockMovement.quantity), 0)).filter(
            StockMovement.type == MovementType.STOCK_IN,
            StockMovement.created_at >= day_start,
            StockMovement.created_at < day_end,
        ).scalar() or 0

        day_out = db.query(func.coalesce(func.abs(func.sum(StockMovement.quantity)), 0)).filter(
            StockMovement.type == MovementType.STOCK_OUT,
            StockMovement.created_at >= day_start,
            StockMovement.created_at < day_end,
        ).scalar() or 0

        in_data.append(int(day_in))
        out_data.append(int(day_out))

    return {"labels": labels, "in": in_data, "out": out_data}


# ═══════════════════════════════════════════════════════════════════
#  TELEGRAM WEBHOOK (for the bot)
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/webhook/telegram")
async def telegram_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Telegram bot webhook endpoint.
    The bot process forwards incoming messages here for stock processing.
    """
    data = await request.json()
    message = data.get("message", {})
    text = data.get("caption") or message.get("text") or ""
    photos = data.get("photo") or message.get("photo", [])
    sender = message.get("from", {})
    sender_username = sender.get("username", "")

    # Parse text: "Item Name 25" -> name="Item Name", qty=25
    parts = text.strip().split()
    if len(parts) < 2:
        return {"ok": False, "error": "Format: [nama barang] [jumlah]"}

    try:
        quantity = int(parts[-1])
    except ValueError:
        return {"ok": False, "error": "Jumlah harus angka. Format: [nama barang] [jumlah]"}

    product_name = " ".join(parts[:-1])

    # Find or create product
    product = db.query(Product).filter(Product.name.ilike(product_name)).first()
    is_new = False
    if not product:
        product = Product(name=product_name, current_stock=0)
        db.add(product)
        db.flush()
        is_new = True

    before = product.current_stock
    product.current_stock += quantity

    movement = StockMovement(
        product_id=product.id,
        type=MovementType.STOCK_IN,
        source=MovementSource.TELEGRAM,
        quantity=quantity,
        stock_before=before,
        stock_after=product.current_stock,
        notes=f"Telegram: @{sender_username}",
    )
    db.add(movement)
    db.commit()

    return {
        "ok": True,
        "product": product_name,
        "is_new": is_new,
        "quantity": quantity,
        "stock_before": before,
        "stock_after": product.current_stock,
    }


# ── Main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=True)

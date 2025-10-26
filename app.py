from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx
import secrets
import sqlite3
import hashlib
from datetime import datetime, timedelta
from typing import Optional
import os
import string

app = FastAPI(title="MK AI API", description="Advanced AI API with credit limits")

# Create templates directory
os.makedirs("templates", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Database initialization
def init_db():
    conn = sqlite3.connect('ai_api.db')
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1,
            total_requests INTEGER DEFAULT 0,
            daily_requests INTEGER DEFAULT 0,
            daily_limit INTEGER DEFAULT 30,
            last_reset TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            prompt TEXT,
            response_time FLOAT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    password_hash = hashlib.sha256("mk123".encode()).hexdigest()
    c.execute('''
        INSERT OR IGNORE INTO admin_users (username, password_hash) 
        VALUES (?, ?)
    ''', ('mk', password_hash))
    
    conn.commit()
    conn.close()

init_db()

# Utility functions
def generate_short_api_key():
    """Generate 8-character API key starting with 'mk'"""
    chars = string.ascii_uppercase + string.digits
    random_chars = ''.join(secrets.choice(chars) for _ in range(6))
    return f"mk{random_chars}"

def get_db_connection():
    conn = sqlite3.connect('ai_api.db')
    conn.row_factory = sqlite3.Row
    return conn

def verify_admin(username: str, password: str) -> bool:
    conn = get_db_connection()
    admin = conn.execute(
        'SELECT password_hash FROM admin_users WHERE username = ?', 
        (username,)
    ).fetchone()
    conn.close()
    return admin and hashlib.sha256(password.encode()).hexdigest() == admin['password_hash']

def check_daily_limit(api_key: str) -> bool:
    """Check if user has exceeded daily limit"""
    conn = get_db_connection()
    key_data = conn.execute(
        'SELECT daily_requests, daily_limit, last_reset FROM api_keys WHERE key = ?',
        (api_key,)
    ).fetchone()
    
    if not key_data:
        conn.close()
        return False
    
    # Reset daily count if new day
    last_reset = datetime.fromisoformat(key_data['last_reset'])
    if datetime.utcnow().date() > last_reset.date():
        conn.execute(
            'UPDATE api_keys SET daily_requests = 0, last_reset = CURRENT_TIMESTAMP WHERE key = ?',
            (api_key,)
        )
        conn.commit()
        daily_used = 0
    else:
        daily_used = key_data['daily_requests']
    
    conn.close()
    return daily_used < key_data['daily_limit']

def has_user_generated_key(ip_address: str) -> bool:
    """Check if user already generated an API key"""
    conn = get_db_connection()
    existing_key = conn.execute(
        'SELECT id FROM api_keys WHERE ip_address = ?',
        (ip_address,)
    ).fetchone()
    conn.close()
    return existing_key is not None

def log_request(api_key: str, endpoint: str, prompt: str = None, response_time: float = None):
    conn = get_db_connection()
    conn.execute(
        'INSERT INTO request_logs (api_key, endpoint, prompt, response_time) VALUES (?, ?, ?, ?)',
        (api_key, endpoint, prompt, response_time)
    )
    conn.commit()
    conn.close()

def update_usage(api_key: str):
    conn = get_db_connection()
    conn.execute(
        '''UPDATE api_keys 
           SET total_requests = total_requests + 1, 
               daily_requests = daily_requests + 1,
               last_used = CURRENT_TIMESTAMP 
           WHERE key = ?''',
        (api_key,)
    )
    conn.commit()
    conn.close()

# Web Interface Routes
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Main website with API key generation"""
    client_ip = request.client.host
    user_agent = request.headers.get('user-agent', 'Unknown')
    
    conn = get_db_connection()
    user_key = conn.execute(
        'SELECT key, daily_requests, daily_limit FROM api_keys WHERE ip_address = ?',
        (client_ip,)
    ).fetchone()
    
    # System stats
    total_keys = conn.execute('SELECT COUNT(*) FROM api_keys').fetchone()[0]
    total_requests = conn.execute('SELECT SUM(total_requests) FROM api_keys').fetchone()[0] or 0
    active_today = conn.execute(
        'SELECT COUNT(DISTINCT api_key) FROM request_logs WHERE DATE(created_at) = DATE("now")'
    ).fetchone()[0]
    
    conn.close()
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "user_key": user_key,
        "total_keys": total_keys,
        "total_requests": total_requests,
        "active_today": active_today,
        "has_key": user_key is not None
    })

@app.get("/generate-key")
async def generate_key(request: Request):
    """Generate API key for user (one per IP)"""
    client_ip = request.client.host
    user_agent = request.headers.get('user-agent', 'Unknown')
    
    # Check if user already has a key
    if has_user_generated_key(client_ip):
        raise HTTPException(status_code=400, detail="You already have an API key. One key per user.")
    
    # Generate unique key
    max_attempts = 10
    for _ in range(max_attempts):
        new_key = generate_short_api_key()
        conn = get_db_connection()
        existing = conn.execute('SELECT id FROM api_keys WHERE key = ?', (new_key,)).fetchone()
        
        if not existing:
            # Save the new key
            conn.execute(
                'INSERT INTO api_keys (key, ip_address, user_agent, daily_limit) VALUES (?, ?, ?, ?)',
                (new_key, client_ip, user_agent, 30)
            )
            conn.commit()
            conn.close()
            
            return {
                "success": True,
                "api_key": new_key,
                "message": "API key generated successfully!",
                "daily_limit": 30,
                "note": "Save this key securely. 30 requests per day."
            }
        conn.close()
    
    raise HTTPException(status_code=500, detail="Failed to generate unique API key")

@app.get("/my-api")
async def my_api_info(request: Request):
    """Get user's API key information"""
    client_ip = request.client.host
    
    conn = get_db_connection()
    key_data = conn.execute(
        'SELECT key, total_requests, daily_requests, daily_limit, created_at, last_used FROM api_keys WHERE ip_address = ?',
        (client_ip,)
    ).fetchone()
    
    if not key_data:
        conn.close()
        raise HTTPException(status_code=404, detail="No API key found for your IP")
    
    # Get today's usage
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_requests = conn.execute(
        'SELECT COUNT(*) FROM request_logs WHERE api_key = ? AND created_at >= ?',
        (key_data['key'], today_start)
    ).fetchone()[0]
    
    conn.close()
    
    return {
        "api_key": key_data['key'],
        "usage": {
            "total_requests": key_data['total_requests'],
            "used_today": today_requests,
            "daily_limit": key_data['daily_limit'],
            "remaining_today": max(0, key_data['daily_limit'] - today_requests)
        },
        "created_at": key_data['created_at'],
        "last_used": key_data['last_used']
    }

# API Routes
@app.get("/text")
async def text_generation(
    prompt: str = Query(..., description="Text to send to AI"),
    api_key: str = Query(..., description="Your API key")
):
    """Text generation using Pollinations.ai"""
    start_time = datetime.utcnow()
    
    if not check_daily_limit(api_key):
        raise HTTPException(status_code=429, detail="Daily limit exceeded. 30 requests per day.")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            pollinations_url = f"https://text.pollinations.ai/prompt/{prompt}"
            response = await client.get(pollinations_url)
            response.raise_for_status()
            ai_response = response.text
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI service error: {str(e)}")
    
    response_time = (datetime.utcnow() - start_time).total_seconds()
    update_usage(api_key)
    log_request(api_key, "/text", prompt, response_time)
    
    return ai_response

@app.get("/image")
async def image_generation(
    prompt: str = Query(..., description="Image generation prompt"),
    api_key: str = Query(..., description="Your API key"),
    width: int = Query(512, description="Image width"),
    height: int = Query(512, description="Image height")
):
    """Image generation using Pollinations.ai"""
    start_time = datetime.utcnow()
    
    if not check_daily_limit(api_key):
        raise HTTPException(status_code=429, detail="Daily limit exceeded. 30 requests per day.")
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            pollinations_url = f"https://image.pollinations.ai/prompt/{prompt}"
            params = {"width": width, "height": height}
            response = await client.get(pollinations_url, params=params)
            response.raise_for_status()
            
            # Return image information
            return {
                "image_url": f"https://image.pollinations.ai/prompt/{prompt}?width={width}&height={height}",
                "prompt": prompt,
                "dimensions": f"{width}x{height}",
                "note": "Visit the URL to see your generated image"
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image service error: {str(e)}")
    
    response_time = (datetime.utcnow() - start_time).total_seconds()
    update_usage(api_key)
    log_request(api_key, "/image", prompt, response_time)

@app.get("/usage")
async def check_usage(api_key: str = Query(..., description="Your API key")):
    """Check API key usage"""
    conn = get_db_connection()
    key_data = conn.execute(
        'SELECT * FROM api_keys WHERE key = ?',
        (api_key,)
    ).fetchone()
    
    if not key_data:
        conn.close()
        raise HTTPException(status_code=404, detail="API key not found")
    
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_requests = conn.execute(
        'SELECT COUNT(*) FROM request_logs WHERE api_key = ? AND created_at >= ?',
        (api_key, today_start)
    ).fetchone()[0]
    
    conn.close()
    
    return {
        "api_key": api_key,
        "usage": {
            "total_requests": key_data['total_requests'],
            "used_today": today_requests,
            "daily_limit": key_data['daily_limit'],
            "remaining_today": max(0, key_data['daily_limit'] - today_requests)
        },
        "created_at": key_data['created_at'],
        "last_used": key_data['last_used']
    }

# Admin Routes
@app.get("/admin/keys")
async def admin_list_keys(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password")
):
    """Admin: List all API keys"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    conn = get_db_connection()
    keys = conn.execute('SELECT * FROM api_keys ORDER BY created_at DESC').fetchall()
    
    keys_list = []
    for key in keys:
        today_requests = conn.execute(
            'SELECT COUNT(*) FROM request_logs WHERE api_key = ? AND DATE(created_at) = DATE("now")',
            (key['key'],)
        ).fetchone()[0]
        
        keys_list.append({
            "id": key['id'],
            "key": key['key'],
            "ip_address": key['ip_address'],
            "total_requests": key['total_requests'],
            "used_today": today_requests,
            "daily_limit": key['daily_limit'],
            "created_at": key['created_at'],
            "last_used": key['last_used'],
            "is_active": bool(key['is_active'])
        })
    
    conn.close()
    
    return {
        "total_keys": len(keys_list),
        "keys": keys_list
    }

@app.get("/admin/reset-limit")
async def admin_reset_limit(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password"),
    api_key: str = Query(..., description="API key to reset")
):
    """Admin: Reset daily limit for a key"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    conn = get_db_connection()
    conn.execute(
        'UPDATE api_keys SET daily_requests = 0, last_reset = CURRENT_TIMESTAMP WHERE key = ?',
        (api_key,)
    )
    conn.commit()
    conn.close()
    
    return {"success": True, "message": f"Daily limit reset for {api_key}"}

@app.get("/admin/delete-key")
async def admin_delete_key(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password"),
    api_key: str = Query(..., description="API key to delete")
):
    """Admin: Delete an API key"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    conn = get_db_connection()
    conn.execute('DELETE FROM request_logs WHERE api_key = ?', (api_key,))
    conn.execute('DELETE FROM api_keys WHERE key = ?', (api_key,))
    conn.commit()
    conn.close()
    
    return {"success": True, "message": f"API key {api_key} deleted"}

@app.get("/admin/stats")
async def admin_stats(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password")
):
    """Admin: System statistics"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    conn = get_db_connection()
    
    total_keys = conn.execute('SELECT COUNT(*) FROM api_keys').fetchone()[0]
    total_requests = conn.execute('SELECT SUM(total_requests) FROM api_keys').fetchone()[0] or 0
    active_today = conn.execute(
        'SELECT COUNT(DISTINCT api_key) FROM request_logs WHERE DATE(created_at) = DATE("now")'
    ).fetchone()[0]
    
    conn.close()
    
    return {
        "total_users": total_keys,
        "total_requests": total_requests,
        "active_today": active_today
    }

@app.get("/docs")
async def documentation():
    """API Documentation"""
    return {
        "endpoints": {
            "GET /": "Web interface with API key generation",
            "GET /generate-key": "Generate your API key (one per user)",
            "GET /my-api": "Check your API key and usage",
            "GET /text?prompt=...&api_key=...": "Text generation (30/day)",
            "GET /image?prompt=...&api_key=...": "Image generation (30/day)",
            "GET /usage?api_key=...": "Check your usage",
            "GET /admin/...": "Admin endpoints (requires auth)"
        },
        "note": "API keys start with 'mk' and are 8 characters long. 30 requests per day limit."
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

from fastapi import FastAPI, HTTPException, Query
import httpx
import secrets
import sqlite3
import hashlib
from datetime import datetime, timedelta
import os
import string

app = FastAPI(
    title="MK AI API",
    description="Advanced AI API with 30 daily requests limit",
    version="3.0.0"
)

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
        'SELECT daily_requests, daily_limit, last_reset FROM api_keys WHERE key = ? AND is_active = 1',
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

# Public API Routes
@app.get("/")
async def root():
    """Welcome message with API information"""
    return {
        "message": "🚀 Welcome to MK AI API",
        "description": "Advanced AI services with 30 daily requests limit",
        "version": "3.0.0",
        "endpoints": {
            "GET /generate-key": "Generate your API key (one per IP)",
            "GET /text?prompt=...&api_key=...": "Text generation",
            "GET /image?prompt=...&api_key=...": "Image generation", 
            "GET /usage?api_key=...": "Check your usage",
            "GET /docs": "Interactive API documentation",
            "Admin endpoints": "Available with admin credentials"
        },
        "limits": {
            "daily_requests": 30,
            "key_format": "8 characters starting with 'mk'",
            "one_key_per_user": True
        }
    }

@app.get("/generate-key")
async def generate_key(ip_address: str = Query(..., description="User IP address")):
    """Generate API key for user (one per IP)"""
    
    # Check if user already has a key
    if has_user_generated_key(ip_address):
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
                'INSERT INTO api_keys (key, ip_address, daily_limit) VALUES (?, ?, ?)',
                (new_key, ip_address, 30)
            )
            conn.commit()
            conn.close()
            
            return {
                "success": True,
                "api_key": new_key,
                "message": "API key generated successfully!",
                "daily_limit": 30,
                "note": "Save this key securely. 30 requests per day. One key per user.",
                "usage_example": f"/text?prompt=Hello&api_key={new_key}"
            }
        conn.close()
    
    raise HTTPException(status_code=500, detail="Failed to generate unique API key")

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
    
    # Return ONLY the AI response (clean format)
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
            image_response = {
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
    
    return image_response

@app.get("/usage")
async def check_usage(api_key: str = Query(..., description="Your API key")):
    """Check API key usage and limits"""
    conn = get_db_connection()
    key_data = conn.execute(
        'SELECT * FROM api_keys WHERE key = ?',
        (api_key,)
    ).fetchone()
    
    if not key_data:
        conn.close()
        raise HTTPException(status_code=404, detail="API key not found")
    
    # Get today's usage
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_requests = conn.execute(
        'SELECT COUNT(*) FROM request_logs WHERE api_key = ? AND created_at >= ?',
        (api_key, today_start)
    ).fetchone()[0]
    
    conn.close()
    
    return {
        "api_key": api_key,
        "is_active": bool(key_data['is_active']),
        "usage": {
            "total_requests": key_data['total_requests'],
            "used_today": today_requests,
            "daily_limit": key_data['daily_limit'],
            "remaining_today": max(0, key_data['daily_limit'] - today_requests)
        },
        "created_at": key_data['created_at'],
        "last_used": key_data['last_used'],
        "next_reset": "Midnight UTC"
    }

@app.get("/key-info")
async def get_key_info(api_key: str = Query(..., description="Your API key")):
    """Get detailed information about your API key"""
    conn = get_db_connection()
    key_data = conn.execute(
        'SELECT * FROM api_keys WHERE key = ?',
        (api_key,)
    ).fetchone()
    
    if not key_data:
        conn.close()
        raise HTTPException(status_code=404, detail="API key not found")
    
    # Get usage statistics
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    
    today_requests = conn.execute(
        'SELECT COUNT(*) FROM request_logs WHERE api_key = ? AND created_at >= ?',
        (api_key, today_start)
    ).fetchone()[0]
    
    week_requests = conn.execute(
        'SELECT COUNT(*) FROM request_logs WHERE api_key = ? AND created_at >= ?',
        (api_key, week_start)
    ).fetchone()[0]
    
    # Get recent requests
    recent_requests = conn.execute(
        'SELECT endpoint, prompt, created_at FROM request_logs WHERE api_key = ? ORDER BY created_at DESC LIMIT 5',
        (api_key,)
    ).fetchall()
    
    conn.close()
    
    return {
        "api_key": api_key,
        "status": "active" if key_data['is_active'] else "inactive",
        "statistics": {
            "total_requests": key_data['total_requests'],
            "today_requests": today_requests,
            "week_requests": week_requests,
            "daily_limit": key_data['daily_limit'],
            "remaining_today": max(0, key_data['daily_limit'] - today_requests)
        },
        "timestamps": {
            "created_at": key_data['created_at'],
            "last_used": key_data['last_used'],
            "last_reset": key_data['last_reset']
        },
        "recent_activity": [
            {
                "endpoint": req['endpoint'],
                "prompt": req['prompt'][:50] + "..." if req['prompt'] and len(req['prompt']) > 50 else req['prompt'],
                "time": req['created_at']
            }
            for req in recent_requests
        ]
    }

# Admin Routes
@app.get("/admin/generate-key")
async def admin_generate_key(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password"),
    ip_address: str = Query(..., description="User IP address"),
    daily_limit: int = Query(30, description="Daily request limit")
):
    """Admin: Generate API key for specific user"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    # Generate unique key
    new_key = generate_short_api_key()
    conn = get_db_connection()
    
    try:
        conn.execute(
            'INSERT INTO api_keys (key, ip_address, daily_limit) VALUES (?, ?, ?)',
            (new_key, ip_address, daily_limit)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Key generation failed")
    
    conn.close()
    
    return {
        "success": True,
        "api_key": new_key,
        "ip_address": ip_address,
        "daily_limit": daily_limit,
        "message": "API key generated successfully"
    }

@app.get("/admin/list-keys")
async def admin_list_keys(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password")
):
    """Admin: List all API keys with detailed information"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    conn = get_db_connection()
    keys = conn.execute('SELECT * FROM api_keys ORDER BY created_at DESC').fetchall()
    
    keys_list = []
    for key in keys:
        # Get today's usage
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_requests = conn.execute(
            'SELECT COUNT(*) FROM request_logs WHERE api_key = ? AND created_at >= ?',
            (key['key'], today_start)
        ).fetchone()[0]
        
        keys_list.append({
            "id": key['id'],
            "key": key['key'],
            "ip_address": key['ip_address'],
            "is_active": bool(key['is_active']),
            "total_requests": key['total_requests'],
            "today_requests": today_requests,
            "daily_limit": key['daily_limit'],
            "created_at": key['created_at'],
            "last_used": key['last_used'],
            "last_reset": key['last_reset']
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
    """Admin: Reset daily limit for specific key"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    conn = get_db_connection()
    key_data = conn.execute('SELECT * FROM api_keys WHERE key = ?', (api_key,)).fetchone()
    
    if not key_data:
        conn.close()
        raise HTTPException(status_code=404, detail="API key not found")
    
    conn.execute(
        'UPDATE api_keys SET daily_requests = 0, last_reset = CURRENT_TIMESTAMP WHERE key = ?',
        (api_key,)
    )
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": f"Daily limit reset for {api_key}",
        "reset_at": datetime.utcnow().isoformat()
    }

@app.get("/admin/increase-limit")
async def admin_increase_limit(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password"),
    api_key: str = Query(..., description="API key to modify"),
    new_limit: int = Query(50, description="New daily limit")
):
    """Admin: Increase daily limit for specific key"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    conn = get_db_connection()
    key_data = conn.execute('SELECT * FROM api_keys WHERE key = ?', (api_key,)).fetchone()
    
    if not key_data:
        conn.close()
        raise HTTPException(status_code=404, detail="API key not found")
    
    conn.execute(
        'UPDATE api_keys SET daily_limit = ? WHERE key = ?',
        (new_limit, api_key)
    )
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": f"Daily limit increased to {new_limit} for {api_key}",
        "previous_limit": key_data['daily_limit'],
        "new_limit": new_limit
    }

@app.get("/admin/delete-key")
async def admin_delete_key(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password"),
    api_key: str = Query(..., description="API key to delete")
):
    """Admin: Delete API key and all its logs"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    conn = get_db_connection()
    key_data = conn.execute('SELECT * FROM api_keys WHERE key = ?', (api_key,)).fetchone()
    
    if not key_data:
        conn.close()
        raise HTTPException(status_code=404, detail="API key not found")
    
    # Delete associated logs
    conn.execute('DELETE FROM request_logs WHERE api_key = ?', (api_key,))
    # Delete the key
    conn.execute('DELETE FROM api_keys WHERE key = ?', (api_key,))
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": f"API key {api_key} deleted successfully",
        "deleted_key": api_key,
        "total_requests_removed": key_data['total_requests']
    }

@app.get("/admin/stats")
async def admin_stats(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password")
):
    """Admin: System-wide statistics"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    conn = get_db_connection()
    
    # Basic stats
    total_keys = conn.execute('SELECT COUNT(*) FROM api_keys').fetchone()[0]
    active_keys = conn.execute('SELECT COUNT(*) FROM api_keys WHERE is_active = 1').fetchone()[0]
    total_requests = conn.execute('SELECT SUM(total_requests) FROM api_keys').fetchone()[0] or 0
    
    # Today's stats
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_requests = conn.execute(
        'SELECT COUNT(*) FROM request_logs WHERE created_at >= ?',
        (today_start,)
    ).fetchone()[0]
    
    # Top users today
    top_users = conn.execute('''
        SELECT api_key, COUNT(*) as request_count 
        FROM request_logs 
        WHERE created_at >= ? 
        GROUP BY api_key 
        ORDER BY request_count DESC 
        LIMIT 5
    ''', (today_start,)).fetchall()
    
    conn.close()
    
    return {
        "system_overview": {
            "total_users": total_keys,
            "active_users": active_keys,
            "total_requests_all_time": total_requests,
            "requests_today": today_requests
        },
        "top_users_today": [
            {"api_key": user['api_key'], "requests": user['request_count']}
      

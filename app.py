from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
import httpx
import secrets
import sqlite3
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import os
import json

app = FastAPI(
    title="Universal AI API",
    description="Multi-service AI API with credit limits and admin controls",
    version="3.0.0"
)

# Database initialization
def init_db():
    conn = sqlite3.connect('ai_api.db')
    c = conn.cursor()
    
    # API keys table with credit limits
    c.execute('''
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            name TEXT DEFAULT 'User Key',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1,
            total_requests INTEGER DEFAULT 0,
            daily_requests INTEGER DEFAULT 0,
            daily_limit INTEGER DEFAULT 30,
            last_reset TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP,
            expires_at TIMESTAMP
        )
    ''')
    
    # Admin users table
    c.execute('''
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    
    # Request logs table
    c.execute('''
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            prompt TEXT,
            response_time FLOAT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_key) REFERENCES api_keys (key)
        )
    ''')
    
    # Insert default admin
    password_hash = hashlib.sha256("mk123".encode()).hexdigest()
    c.execute('''
        INSERT OR IGNORE INTO admin_users (username, password_hash) 
        VALUES (?, ?)
    ''', ('mk', password_hash))
    
    conn.commit()
    conn.close()

init_db()

# Utility functions
def generate_api_key():
    return f"api_{secrets.token_urlsafe(24)}"

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
    """Check if user has exceeded daily limit and reset if new day"""
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

def log_request(api_key: str, endpoint: str, prompt: str = None, response_time: float = None):
    """Log API request for analytics"""
    conn = get_db_connection()
    conn.execute(
        'INSERT INTO request_logs (api_key, endpoint, prompt, response_time) VALUES (?, ?, ?, ?)',
        (api_key, endpoint, prompt, response_time)
    )
    conn.commit()
    conn.close()

def update_usage(api_key: str):
    """Update usage statistics"""
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

# API Routes
@app.get("/")
async def root():
    """Public API access with auto-generated key"""
    # Generate a temporary key for public use
    temp_key = generate_api_key()
    
    conn = get_db_connection()
    conn.execute(
        'INSERT INTO api_keys (key, name, daily_limit) VALUES (?, ?, ?)',
        (temp_key, 'Public User', 30)
    )
    conn.commit()
    conn.close()
    
    return {
        "message": "Welcome to Universal AI API!",
        "your_api_key": temp_key,
        "daily_limit": 30,
        "endpoints": {
            "/docs": "Interactive API documentation",
            "/api_key": "Check your API usage",
            "/text": "Text generation with Pollinations.ai",
            "/image": "Image generation with Pollinations.ai",
            "/admin/...": "Admin controls (requires authentication)"
        },
        "note": "This key has 30 daily requests. Use /api_key to check usage."
    }

@app.get("/api_key")
async def check_api_usage(api_key: str = Query(..., description="Your API key")):
    """Check API key usage and limits"""
    conn = get_db_connection()
    key_data = conn.execute(
        'SELECT * FROM api_keys WHERE key = ?',
        (api_key,)
    ).fetchone()
    
    if not key_data:
        conn.close()
        raise HTTPException(status_code=404, detail="API key not found")
    
    # Get today's usage from logs
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_requests = conn.execute(
        'SELECT COUNT(*) FROM request_logs WHERE api_key = ? AND created_at >= ?',
        (api_key, today_start)
    ).fetchone()[0]
    
    conn.close()
    
    return {
        "api_key": f"{api_key[:8]}...{api_key[-4:]}",
        "name": key_data['name'],
        "is_active": bool(key_data['is_active']),
        "usage": {
            "total_requests": key_data['total_requests'],
            "daily_used": today_requests,
            "daily_limit": key_data['daily_limit'],
            "remaining_today": max(0, key_data['daily_limit'] - today_requests)
        },
        "created_at": key_data['created_at'],
        "last_used": key_data['last_used']
    }

@app.get("/text")
async def text_generation(
    prompt: str = Query(..., description="Text to send to AI"),
    api_key: str = Query(..., description="Your API key")
):
    """Text generation using Pollinations.ai"""
    start_time = datetime.utcnow()
    
    # Validate API key and check limits
    if not check_daily_limit(api_key):
        raise HTTPException(status_code=429, detail="Daily limit exceeded. 30 requests per day.")
    
    # Call Pollinations.ai
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            pollinations_url = f"https://text.pollinations.ai/prompt/{prompt}"
            response = await client.get(pollinations_url)
            response.raise_for_status()
            ai_response = response.text
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI service error: {str(e)}")
    
    # Update usage and log request
    response_time = (datetime.utcnow() - start_time).total_seconds()
    update_usage(api_key)
    log_request(api_key, "/text", prompt, response_time)
    
    # Return ONLY the AI response
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
    
    # Validate API key and check limits
    if not check_daily_limit(api_key):
        raise HTTPException(status_code=429, detail="Daily limit exceeded. 30 requests per day.")
    
    # Call Pollinations.ai Image API
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            pollinations_url = f"https://image.pollinations.ai/prompt/{prompt}"
            params = {"width": width, "height": height}
            response = await client.get(pollinations_url, params=params)
            response.raise_for_status()
            
            # Return image URL or data
            image_data = response.content
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image service error: {str(e)}")
    
    # Update usage and log request
    response_time = (datetime.utcnow() - start_time).total_seconds()
    update_usage(api_key)
    log_request(api_key, "/image", prompt, response_time)
    
    # For now, return the image URL structure
    return {
        "image_url": f"https://image.pollinations.ai/prompt/{prompt}",
        "prompt": prompt,
        "dimensions": f"{width}x{height}"
    }

# Admin Routes
@app.get("/admin/generateapi")
async def admin_generate_key(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password"),
    key_name: str = Query("User Key", description="Name for the key"),
    daily_limit: int = Query(30, description="Daily request limit")
):
    """Admin: Generate new API key"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    new_key = generate_api_key()
    expires_at = datetime.utcnow() + timedelta(days=365)
    
    conn = get_db_connection()
    try:
        conn.execute(
            'INSERT INTO api_keys (key, name, daily_limit, expires_at) VALUES (?, ?, ?, ?)',
            (new_key, key_name, daily_limit, expires_at)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Key generation failed")
    
    conn.close()
    
    return {
        "success": True,
        "api_key": new_key,
        "key_name": key_name,
        "daily_limit": daily_limit,
        "expires_at": expires_at.isoformat()
    }

@app.get("/admin/listapi")
async def admin_list_keys(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password")
):
    """Admin: List all API keys with detailed information"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    conn = get_db_connection()
    keys = conn.execute('SELECT * FROM api_keys ORDER BY created_at DESC').fetchall()
    
    # Get detailed statistics
    keys_with_stats = []
    for key in keys:
        # Get today's usage
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_requests = conn.execute(
            'SELECT COUNT(*) FROM request_logs WHERE api_key = ? AND created_at >= ?',
            (key['key'], today_start)
        ).fetchone()[0]
        
        keys_with_stats.append({
            "id": key['id'],
            "name": key['name'],
            "key": key['key'],
            "is_active": bool(key['is_active']),
            "total_requests": key['total_requests'],
            "daily_used": today_requests,
            "daily_limit": key['daily_limit'],
            "created_at": key['created_at'],
            "last_used": key['last_used'],
            "expires_at": key['expires_at']
        })
    
    conn.close()
    
    return {
        "total_keys": len(keys_with_stats),
        "keys": keys_with_stats
    }

@app.get("/admin/increaseapilimit")
async def admin_increase_limit(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password"),
    api_key: str = Query(..., description="API key to modify"),
    new_limit: int = Query(50, description="New daily limit")
):
    """Admin: Increase daily limit for an API key"""
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
        "message": f"Daily limit increased to {new_limit} for key {api_key[:8]}...",
        "new_limit": new_limit
    }

@app.get("/admin/resetapilimit")
async def admin_reset_limit(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password"),
    api_key: str = Query(..., description="API key to reset")
):
    """Admin: Reset daily usage counter"""
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
        "message": f"Daily limit reset for key {api_key[:8]}...",
        "reset_at": datetime.utcnow().isoformat()
    }

@app.get("/admin/deleteapi")
async def admin_delete_key(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password"),
    api_key: str = Query(..., description="API key to delete")
):
    """Admin: Delete an API key"""
    if not verify_admin(admin_username, admin_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    conn = get_db_connection()
    key_data = conn.execute('SELECT * FROM api_keys WHERE key = ?', (api_key,)).fetchone()
    
    if not key_data:
        conn.close()
        raise HTTPException(status_code=404, detail="API key not found")
    
    # Delete associated logs first
    conn.execute('DELETE FROM request_logs WHERE api_key = ?', (api_key,))
    # Delete the key
    conn.execute('DELETE FROM api_keys WHERE key = ?', (api_key,))
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": f"API key {api_key[:8]}... deleted successfully",
        "deleted_key": api_key[:8] + "...",
        "total_requests": key_data['total_requests']
    }

@app.get("/admin/stats")
async def admin_stats(
    admin_username: str = Query(..., description="Admin username"),
    admin_password: str = Query(..., description="Admin password")
):
    """Admin: Overall system statistics"""
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
    
    # Top users
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
        "system_stats": {
            "total_api_keys": total_keys,
            "active_keys": active_keys,
            "total_requests_all_time": total_requests,
            "requests_today": today_requests
        },
        "top_users_today": [
            {"api_key": user['api_key'][:8] + "...", "requests": user['request_count']}
            for user in top_users
        ]
    }

@app.get("/docs")
async def documentation():
    """Interactive API documentation"""
    return RedirectResponse("/docs")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

import asyncio
import sqlite3
import psycopg2
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import aiohttp
import json
import secrets
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import razorpay

load_dotenv()

# ⚙️ CONFIG
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("8013912448", 0))
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")  # Cloud backup

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# 💳 Razorpay Client
razorpay_client = razorpay.Client(
    auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
)

class AdminStates(StatesGroup):
    adding_product = State()
    setting_stock = State()
    setting_owner = State()

class BuyStates(StatesGroup):
    selecting_product = State()
    payment_pending = State()

class SafeDatabase:
    def __init__(self):
        self.local_db = sqlite3.connect("safe_store.db", check_same_thread=False)
        self.cloud_conn = None
        self.init_db()
    
    def init_db(self):
        cursor = self.local_db.cursor()
        # Products table
        cursor.execute('''CREATE TABLE IF NOT EXISTS products 
            (id INTEGER PRIMARY KEY AUTOINCREMENT, 
             name TEXT NOT NULL, 
             price REAL NOT NULL, 
             stock INTEGER NOT NULL, 
             keys TEXT,
             updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        # Orders table  
        cursor.execute('''CREATE TABLE IF NOT EXISTS orders 
            (id INTEGER PRIMARY KEY AUTOINCREMENT,
             user_id BIGINT NOT NULL,
             username TEXT,
             product_id INTEGER,
             product_name TEXT,
             amount REAL,
             status TEXT DEFAULT 'pending',
             razorpay_order_id TEXT,
             key_delivered TEXT,
             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        # Errors table
        cursor.execute('''CREATE TABLE IF NOT EXISTS errors 
            (id INTEGER PRIMARY KEY AUTOINCREMENT,
             error TEXT,
             timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        # Insert sample products if empty
        cursor.execute("SELECT COUNT(*) FROM products")
        if cursor.fetchone()[0] == 0:
            sample_products = [
                ("Premium VPN 1 Month", 299.0, 100, ""),
                ("Game Unlocker", 199.0, 50, ""),
                ("Software License", 499.0, 25, "")
            ]
            cursor.executemany("INSERT INTO products (name, price, stock, keys) VALUES (?, ?, ?, ?)", sample_products)
        
        self.local_db.commit()
    
    async def connect_cloud(self):
        if DATABASE_URL:
            try:
                self.cloud_conn = psycopg2.connect(DATABASE_URL)
                print("☁️ Cloud DB Connected!")
            except:
                print("⚠️ Cloud DB failed, using local only")
    
    def get_products(self):
        cursor = self.local_db.cursor()
        cursor.execute("SELECT * FROM products WHERE stock > 0 ORDER BY price")
        return cursor.fetchall()
    
    def get_product(self, product_id):
        cursor = self.local_db.cursor()
        cursor.execute("SELECT * FROM products WHERE id=?", (product_id,))
        return cursor.fetchone()
    
    def get_orders(self, user_id=None, limit=50):
        cursor = self.local_db.cursor()
        if user_id:
            cursor.execute("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        else:
            cursor.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))
        return cursor.fetchall()
    
    def save_order(self, **kwargs):
        cursor = self.local_db.cursor()
        cursor.execute('''INSERT INTO orders 
            (user_id, username, product_id, product_name, amount, status, razorpay_order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)''', 
            (kwargs['user_id'], kwargs['username'], kwargs['product_id'], 
             kwargs['product_name'], kwargs['amount'], kwargs['status'], kwargs.get('razorpay_id')))
        self.local_db.commit()
    
    def update_order_key(self, razorpay_id, key):
        cursor = self.local_db.cursor()
        cursor.execute("UPDATE orders SET status='completed', key_delivered=? WHERE razorpay_order_id=?", (key, razorpay_id))
        self.local_db.commit()
    
    def update_stock(self, product_id, new_stock):
        cursor = self.local_db.cursor()
        cursor.execute("UPDATE products SET stock=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_stock, product_id))
        self.local_db.commit()
    
    def log_error(self, error):
        cursor = self.local_db.cursor()
        cursor.execute("INSERT INTO errors (error) VALUES (?)", (str(error),))
        self.local_db.commit()
    
    def get_stats(self):
        cursor = self.local_db.cursor()
        cursor.execute("SELECT COUNT(*), SUM(amount) FROM orders WHERE status='completed'")
        total_orders, revenue = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM products WHERE stock < 10")
        low_stock = cursor.fetchone()[0]
        return {
            'total_orders': total_orders or 0,
            'revenue': revenue or 0,
            'low_stock': low_stock
        }
    
    async def auto_sync(self):
        """हर 5 मिनट cloud sync"""
        while True:
            await asyncio.sleep(300)
            if self.cloud_conn:
                try:
                    cursor = self.cloud_conn.cursor()
                    # Sync products
                    local_products = self.get_products()
                    for product in local_products:
                        cursor.execute("""
                            INSERT INTO products (id, name, price, stock, keys, updated_at) 
                            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET 
                            stock=EXCLUDED.stock, updated_at=EXCLUDED.updated_at
                        """, product)
                    self.cloud_conn.commit()
                    print("☁️ Synced!")
                except Exception as e:
                    print(f"Sync error: {e}")

# 🌟 Global DB
db = SafeDatabase()

# ⌨️ Keyboards
def main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="🛒 Catalog"), KeyboardButton(text="📋 My Orders"))
    if OWNER_ID == 0:
        builder.row(KeyboardButton(text="👑 Set Owner"))
    return builder.as_markup(resize_keyboard=True)

def catalog_keyboard(products):
    builder = InlineKeyboardBuilder()
    for product in products:
        builder.row(InlineKeyboardButton(
            text=f"{product[1]} - ₹{product[2]} ({product[3]} left)",
            callback_data=f"buy_{product[0]}"
        ))
    builder.row(InlineKeyboardButton(text="🔙 Back", callback_data="back_main"))
    return builder.as_markup()

def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Add Product", callback_data="admin_add"))
    builder.row(InlineKeyboardButton(text="📦 List Products", callback_data="admin_list"))
    builder.row(InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats"))
    builder.row(InlineKeyboardButton(text="🔙 Main", callback_data="back_main"))
    return builder.as_markup()

# 🔔 Owner Notifications
async def notify_owner(message):
    if OWNER_ID:
        try:
            await bot.send_message(OWNER_ID, f"👑 **Owner Alert**\n\n{message}", parse_mode="Markdown")
        except:
            pass

# /start
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(
        "🛒 **Welcome to Safe Store!**\n\n"
        "💾 **100% Data Safe** - Local + Cloud Backup\n"
        "💳 Razorpay Payment\n"
        "🔑 Instant Key Delivery\n\n"
        "Choose from menu below:",
        reply_markup=main_keyboard()
    )

# 🛒 Catalog
@dp.message(F.text == "🛒 Catalog")
async def catalog_handler(message: types.Message):
    products = db.get_products()
    if not products:
        await message.answer("📭 No products available!")
        return
    
    keyboard = catalog_keyboard(products)
    text = "🛒 **Available Products:**\n\n"
    for p in products:
        text += f"• **{p[1]}** - ₹{p[2]} ({p[3]} in stock)\n"
    
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

# Buy Handler
@dp.callback_query(F.data.startswith("buy_"))
async def buy_handler(callback: types.CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split("_")[1])
    product = db.get_product(product_id)
    
    if not product or product[3] <= 0:
        await callback.answer("❌ Out of stock!")
        return
    
    # Save order first (SAFE!)
    db.save_order(
        user_id=callback.from_user.id,
        username=callback.from_user.username or "Unknown",
        product_id=product_id,
        product_name=product[1],
        amount=product[2],
        status="payment_pending"
    )
    
    # Create Razorpay order
    try:
        razorpay_order = razorpay_client.order.create({
            'amount': int(product[2] * 100),  # paise
            'currency': 'INR',
            'receipt': f'order_{callback.from_user.id}_{product_id}'
        })
        
        payment_url = f"https://rzp.io/i/{razorpay_order['id']}"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Pay Now", url=payment_url)],
            [InlineKeyboardButton(text="🔙 Cancel", callback_data="back_catalog")]
        ])
        
        await callback.message.edit_text(
            f"💳 **Pay ₹{product[2]}**\n\n"
            f"Product: **{product[1]}**\n"
            f"Order ID: `{razorpay_order['id']}`\n\n"
            f"Pay above → Send `/check_payment {razorpay_order['id']}`",
            reply_markup=keyboard, parse_mode="Markdown"
        )
        
        await callback.answer("💳 Payment link ready!")
        
    except Exception as e:
        db.log_error(f"Payment error: {e}")
        await callback.answer("❌ Payment failed! Try again.")

# 📋 My Orders
@dp.message(F.text == "📋 My Orders")
async def my_orders(message: types.Message):
    orders = db.get_orders(message.from_user.id, 10)
    
    if not orders:
        await message.answer("📭 No orders found!")
        return
    
    text = "📋 **Your Orders:**\n\n"
    for order in orders:
        status = "✅ Delivered" if order[6] == "completed" else "⏳ Pending"
        key = f"`{order[8]}`" if order[8] else ""
        text += f"**#{order[0]}** | {order[4]} | ₹{order[5]} | {status}\n{key}\n\n"
    
    await message.answer(text, parse_mode="Markdown")

# 💳 Check Payment
@dp.message(Command("check_payment"))
async def check_payment(message: types.Message, state: FSMContext):
    try:
        razorpay_id = message.text.split()[1]
        order_data = razorpay_client.order.fetch(razorpay_id)
        
        if order_data['status'] == 'paid':
            # Find order
            cursor = db.local_db.cursor()
            cursor.execute("SELECT * FROM orders WHERE razorpay_order_id=?", (razorpay_id,))
            order = cursor.fetchone()
            
            if order and order[6] != 'completed':
                # Generate key
                key = secrets.token_urlsafe(32)
                
                # Update order with key
                db.update_order_key(razorpay_id, key)
                
                # Notify owner
                await notify_owner(
                    f"✅ **NEW SALE!**\n\n"
                    f"👤 {order[2]}\n"
                    f"📦 {order[4]}\n"
                    f"💰 ₹{order[5]}\n"
                    f"🆔 Order #{order[0]}"
                )
                
                await message.answer(
                    f"🎉 **Payment Verified!**\n\n"
                    f"✅ Order #{order[0]}\n"
                    f"📦 **{order[4]}**\n"
                    f"🔑 **Your Key:**\n`{key}`\n\n"
                    f"💾 Saved safely!",
                    parse_mode="Markdown"
                )
            else:
                await message.answer("❌ Order not found!")
        else:
            await message.answer("⏳ Payment pending...")
            
    except Exception as e:
        db.log_error(f"Check payment error: {e}")
        await message.answer("❌ Invalid order ID!")

# 👑 Owner Commands
@dp.message(Command("setowner"))
async def set_owner(message: types.Message, state: FSMContext):
    global OWNER_ID
    OWNER_ID = message.from_user.id
    await message.answer("👑 You are now the owner!")
    await notify_owner(f"👑 New owner set: @{message.from_user.username}")

@dp.message(Command("myid"))
async def my_id(message: types.Message):
    await message.answer(f"🆔 Your ID: `{message.from_user.id}`")

# Admin Panel
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("❌ Owner only!")
        return
    await callback.message.edit_text("👑 **Admin Panel**", reply_markup=admin_keyboard())

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID: return
    
    stats = db.get_stats()
    low_stock_products = db.local_db.execute("SELECT * FROM products WHERE stock < 10").fetchall()
    
    text = f"📊 **Store Stats**\n\n"
    text += f"✅ Total Sales: {stats['total_orders']}\n"
    text += f"💰 Revenue: ₹{stats['revenue']:.2f}\n"
    text += f"⚠️ Low Stock: {stats['low_stock']}\n\n"
    
    if low_stock_products:
        text += "🚨 **Low Stock Items:**\n"
        for p in low_stock_products:
            text += f"• {p[1]}: {p[3]} left\n"
    
    await callback.message.edit_text(text, parse_mode="Markdown")

# Error Handler
@dp.errors()
async def errors_handler(event):
    db.log_error(event.exception)
    print(f"Error logged: {event.exception}")

# Back buttons
@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery):
    await callback.message.edit_text("🏠 **Main Menu**", reply_markup=main_keyboard())

# Auto backup task
async def start_auto_backup():
    await db.connect_cloud()
    asyncio.create_task(db.auto_sync())

# 🚀 Start Bot
async def main():
    print("🚀 Starting Safe Store Bot...")
    print(f"💾 Local DB: safe_store.db")
    if DATABASE_URL:
        print("☁️ Cloud backup enabled")
    
    await start_auto_backup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

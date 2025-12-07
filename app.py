import os
import re
import ast
import tempfile
import random
import time
import csv
import requests  # Đã thêm thư viện này
from datetime import datetime
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
import google.generativeai as genai

# -------------------------
# CẤU HÌNH SỰ KIỆN VÒNG QUAY TỬ THẦN
# -------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FOLDER = os.path.join(BASE_DIR, 'data')
os.makedirs(DATA_FOLDER, exist_ok=True)

EVENT_CONFIG = {
    'start_month': 8,    # Tháng 8
    'end_month': 12,      # Tháng 12
    'prizes': [
        {'name': 'Chúc bạn may mắn lần sau', 'value': 0, 'probability': 40},
        {'name': 'Chúc bạn may mắn lần sau', 'value': 0, 'probability': 25},
        {'name': 'Chúc bạn may mắn lần sau', 'value': 0, 'probability': 15},
        {'name': '50,000 VNĐ', 'value': 50000, 'probability': 10},
        {'name': '100,000 VNĐ', 'value': 100000, 'probability': 5},
        {'name': '200,000 VNĐ', 'value': 200000, 'probability': 3},
        {'name': '500,000 VNĐ', 'value': 500000, 'probability': 2}
    ],
    'spend_thresholds': [
        500000,    # Mốc 1: 1 lượt quay
        1000000,   # Mốc 2: 2 lượt quay  
        2000000,   # Mốc 3: 3 lượt quay
        3500000,   # Mốc 4: 4 lượt quay
        5000000    # Mốc 5: 5 lượt quay
    ],
    'rank_bonus_spins': {
        'Đồng': 1,
        'Bạc': 2,
        'Vàng': 3,
        'Bạch kim': 4
    }
}

EVENT_SPINS_CSV = os.path.join(DATA_FOLDER, 'event_spins.csv')
EVENT_PRIZES_CSV = os.path.join(DATA_FOLDER, 'event_prizes.csv')

# -------------------------
# Tạo app Flask
# -------------------------
app = Flask(__name__)
app.secret_key = "your_secret_key_here"

USERS_CSV = "data/users.csv"
BOOKINGS_CSV = "bookings.csv"

# -------------------------
# USER DATABASE
# -------------------------
users_db = {}
bookings_db = []

# -------------------------
# HÀM HỖ TRỢ
# -------------------------
def get_user_rank(total_spent):
    if total_spent >= 20_000_000:
        return "Bạch kim"
    elif total_spent >= 8_000_000:
        return "Vàng"
    elif total_spent >= 3_000_000:
        return "Bạc"
    else:
        return "Đồng"

def get_discounted_price(rank, base_price):
    discount = {"Đồng": 0, "Bạc": 0.05, "Vàng": 0.1, "Bạch kim": 0.2}
    return int(base_price * (1 - discount.get(rank, 0)))

def init_event_files():
    if not os.path.exists(EVENT_SPINS_CSV):
        with open(EVENT_SPINS_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['username', 'spin_date', 'year', 'is_free_spin'])
    
    if not os.path.exists(EVENT_PRIZES_CSV):
        with open(EVENT_PRIZES_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['username', 'prize_value', 'prize_name', 'created_at'])

def calculate_event_spending(username):
    total = 0
    if not os.path.exists(BOOKINGS_CSV):
        return total
    current_year = datetime.now().year
    with open(BOOKINGS_CSV, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row['username'] == username and row['status'].lower() == 'completed'):
                try:
                    booking_time = datetime.strptime(row['booking_time'], '%Y-%m-%d %H:%M:%S')
                    if (booking_time.year == current_year and 
                        EVENT_CONFIG['start_month'] <= booking_time.month <= EVENT_CONFIG['end_month']):
                        total += float(row['price'])
                except (ValueError, KeyError):
                    continue
    return total

def get_max_spins(username):
    user_data = users_db.get(username, {})
    total_spent = user_data.get('total_spent', 0)
    rank = get_user_rank(total_spent)
    
    free_spin = 1
    spend_spins = 0
    for threshold in EVENT_CONFIG['spend_thresholds']:
        if total_spent >= threshold:
            spend_spins += 1
    
    rank_bonus = EVENT_CONFIG['rank_bonus_spins'].get(rank, 0)
    total_spins = free_spin + spend_spins + rank_bonus
    
    print(f"💰 {username}: total_spent={total_spent:,}, spend_spins={spend_spins}, rank={rank}, rank_bonus={rank_bonus}")
    
    return {
        'total_spins': total_spins,
        'free_spin': free_spin,
        'spend_spins': spend_spins,
        'rank_bonus': rank_bonus,
        'rank': rank,
        'total_spent': total_spent
    }

def get_used_spins(username):
    if not os.path.exists(EVENT_SPINS_CSV):
        return 0
    count = 0
    current_year = datetime.now().year
    with open(EVENT_SPINS_CSV, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['username'] == username:
                try:
                    spin_year = int(row['year'])
                    spin_date = datetime.strptime(row['spin_date'], '%Y-%m-%d %H:%M:%S')
                    if (spin_year == current_year and 
                        EVENT_CONFIG['start_month'] <= spin_date.month <= EVENT_CONFIG['end_month']):
                        count += 1
                except (ValueError, KeyError):
                    continue
    return count

def use_spin(username):
    current_year = datetime.now().year
    current_month = datetime.now().month
    
    if not (EVENT_CONFIG['start_month'] <= current_month <= EVENT_CONFIG['end_month']):
        print(f"❌ Không trong thời gian sự kiện: tháng {current_month}")
        return False
    
    spin_info = get_max_spins(username)
    used_spins = get_used_spins(username)
    
    print(f"📊 User {username}: total={spin_info['total_spins']}, used={used_spins}")
    
    if used_spins >= spin_info['total_spins']:
        print(f"❌ {username} đã hết lượt quay")
        return False
    
    is_free_spin = (used_spins == 0)
    
    with open(EVENT_SPINS_CSV, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([username, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), current_year, is_free_spin])
    
    print(f"✅ Đã ghi lượt quay cho {username}, free_spin={is_free_spin}")
    return True

def get_random_prize():
    prizes = []
    for prize in EVENT_CONFIG['prizes']:
        prizes.extend([prize] * prize['probability'])
    return random.choice(prizes)

def update_user_prize(username, prize_value, prize_name):
    with open(EVENT_PRIZES_CSV, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([username, prize_value, prize_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S')])
    
    if username in users_db:
        users_db[username]['total_spent'] += prize_value
        save_users(users_db)
        print(f"✅ Đã cộng {prize_value:,} VNĐ vào total_spent của user {username}")

def generate_booking_code():
    return str(random.randint(10000000, 99999999))

def load_users():
    if not os.path.exists(USERS_CSV):
        df = pd.DataFrame(columns=[
            "username","password","full_name","dob","gender","email","phone","total_spent","history"
        ])
        df.to_csv(USERS_CSV, index=False, encoding="utf-8-sig")
    else:
        df = pd.read_csv(USERS_CSV, encoding="utf-8-sig")
        if "username" not in df.columns:
            df = pd.DataFrame(columns=[
                "username","password","full_name","dob","gender","email","phone","total_spent","history"
            ])
            df.to_csv(USERS_CSV, index=False, encoding="utf-8-sig")

    users = df.set_index('username').T.to_dict()
    for u, data in users.items():
        if 'history' in data:
            try:
                data['history'] = ast.literal_eval(data['history'])
            except:
                data['history'] = []
        else:
            data['history'] = []
    return users

def save_users(users):
    df = pd.DataFrame(users).T
    df['history'] = df['history'].apply(str)
    df.to_csv(USERS_CSV, index_label='username', encoding="utf-8-sig")

users_db = load_users()

# -------------------------
# ROUTES
# -------------------------

@app.route("/")
def index():
    hotels = [
        {"name": "Hotel A", "city": "Đà Nẵng", "price": 3000000},
        {"name": "Hotel B", "city": "Hà Nội", "price": 1500000},
        {"name": "Hotel C", "city": "Hồ Chí Minh", "price": 5000000},
    ]
    user_rank = session.get("user_rank", "Đồng")
    for h in hotels:
        h["price_after_discount"] = get_discounted_price(user_rank, h["price"])
    return render_template("index.html", hotels=hotels, user_rank=user_rank)

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        if username in users_db:
            flash("Tài khoản đã tồn tại!", "danger")
            return redirect(url_for("register"))

        users_db[username] = {
            "password": generate_password_hash(request.form["password"]),
            "full_name": request.form.get("fullname", ""),
            "dob": request.form.get("birthdate", ""),
            "gender": request.form.get("gender", ""),
            "email": request.form.get("email", ""),
            "phone": request.form.get("phone", ""),
            "total_spent": 0,
            "history": []
        }
        
        df = pd.DataFrame(users_db).T
        df.to_csv(USERS_CSV, index_label="username", encoding="utf-8-sig")

        flash("Đăng ký thành công! Hãy đăng nhập.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        user = users_db.get(username)
        if user and check_password_hash(user["password"], password):
            session["user"] = {
                "username": username,
                "email": user["email"],
                "rank": get_user_rank(user["total_spent"])
            }
            flash("Đăng nhập thành công!", "success")
            return redirect(url_for("profile"))
        flash("Sai tài khoản hoặc mật khẩu!", "danger")
        return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Đã đăng xuất!", "success")
    return redirect(url_for("index"))

@app.route("/profile")
def profile():
    if "user" not in session:
        flash("Bạn cần đăng nhập để xem thông tin.", "danger")
        return redirect(url_for("login"))

    user_session = session["user"]
    username = user_session["username"]
    user_data = users_db.get(username, {})

    dob = user_data.get("dob", "")
    age = "-"
    if dob:
        birth = datetime.strptime(dob, "%Y-%m-%d")
        age = int((datetime.now() - birth).days / 365.25)

    if os.path.exists(BOOKINGS_CSV):
        df = pd.read_csv(BOOKINGS_CSV, encoding="utf-8-sig")
        user_history = df[df["email"] == user_data.get("email", "")]
        history = [
            {
                "name": row["hotel_name"],
                "price": "{:,.0f}".format(float(row["price"])),
                "date": row["booking_time"]
            } for idx, row in user_history.iterrows()
        ]
    else:
        history = []

    total_spent = user_data.get("total_spent", 0)

    return render_template(
        "profile.html",
        user=user_data,
        age=age,
        user_rank=user_session.get("rank", "Đồng"),
        total_spent=total_spent,
        history=history
    )

def get_hotel_gallery(hotel_name):
    folder_path = os.path.join("static", "images", "hotels", hotel_name)
    if not os.path.exists(folder_path):
        return []
    files = os.listdir(folder_path)
    return [
        f"/static/images/hotels/{hotel_name}/{f}"
        for f in files if f.lower() not in ["main.jng", "main.png"]
    ]

def read_intro(city_name):
    file_map = {
        "Hà Nội": "hanoi.txt",
        "TP Hồ Chí Minh": "hochiminh.txt",
        "Đà Nẵng": "danang.txt",
        "Nha Trang": "nhatrang.txt"
    }
    filename = file_map.get(city_name)
    if not filename:
        return "❌ Chưa có bài giới thiệu cho địa danh này."
    folder_path = os.path.join("static", "text", "giới thiệu")
    file_path = os.path.join(folder_path, filename)
    if not os.path.exists(file_path):
        return "❌ File giới thiệu chưa được tạo."
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    return content

@app.route("/destinations/<city>")
def destination(city):
    city = city.replace("%20", " ").strip()
    data = {
        "Ha Noi": {"name": "Hà Nội", "desc": "...", "image": "/static/images/destinations/cities/hanoi.png"},
        "Ho Chi Minh": {"name": "TP Hồ Chí Minh", "desc": "...", "image": "/static/images/destinations/cities/hcm.png"},
        "Da Nang": {"name": "Đà Nẵng", "desc": "...", "image": "/static/images/destinations/cities/danang.png"},
        "Nha Trang": {"name": "Nha Trang", "desc": "...", "image": "/static/images/destinations/cities/nhatrang.png"}
    }
    key_map = {
        "hanoi": "Ha Noi",
        "danang": "Da Nang",
        "nhatrang": "Nha Trang",
        "hochiminh": "Ho Chi Minh"
    }
    city_key = data.get(city) or data.get(key_map.get(city.lower(), ""), None)
    if not city_key:
        return "❌ Không tìm thấy địa điểm này", 404
    info = city_key
    info["intro"] = read_intro(info["name"])
    return render_template("destination.html", info=info)

# -------------------------
# CẤU HÌNH FILE CSV VÀ MAIL
# -------------------------
hotels_candidate = os.path.join(BASE_DIR, 'hotels.csv')
if os.path.exists(hotels_candidate):
    HOTELS_CSV = hotels_candidate
else:
    HOTELS_CSV = os.path.join(DATA_FOLDER, 'hotels.csv')

BOOKINGS_CSV = os.path.join(DATA_FOLDER, 'bookings.csv')
REVIEWS_CSV = os.path.join(BASE_DIR, 'reviews.csv') if os.path.exists(os.path.join(BASE_DIR, 'reviews.csv')) else os.path.join(DATA_FOLDER, 'reviews.csv')

app.config.update(
    MAIL_SERVER='smtp.gmail.com',
    MAIL_PORT=587,
    MAIL_USE_TLS=True,
    MAIL_USE_SSL=False,
    MAIL_USERNAME='hotelpinder@gmail.com',
    MAIL_PASSWORD='znsj ynpd burr tdeo',
    MAIL_DEFAULT_SENDER=('Hotel Pinder', 'hotelpinder@gmail.com')
)
mail = Mail(app)

try:
    safe_dir = os.path.dirname(BOOKINGS_CSV)
    os.makedirs(safe_dir, exist_ok=True)
    if not os.path.exists(BOOKINGS_CSV):
        df_empty = pd.DataFrame(columns=[
                "hotel_name", "room_type", "price", "user_name", "phone", "email",
                "num_adults", "num_children", "checkin_date", "nights",
                "special_requests", "booking_time", "status"
        ])
        df_empty.to_csv(BOOKINGS_CSV, index=False, encoding="utf-8-sig")
except Exception as e:
    temp_dir = tempfile.gettempdir()
    BOOKINGS_CSV = os.path.join(temp_dir, "bookings.csv")
    print(f"[⚠] Không thể ghi vào thư mục chính, dùng tạm: {BOOKINGS_CSV}")

if not os.path.exists(HOTELS_CSV):
    raise FileNotFoundError(f"❌ Không tìm thấy hotels.csv — đặt file ở: {HOTELS_CSV}")

if not os.path.exists(REVIEWS_CSV):
    pd.DataFrame(columns=["hotel_name", "user", "rating", "comment"]).to_csv(
        REVIEWS_CSV, index=False, encoding="utf-8-sig"
    )

def read_csv_safe(file_path):
    encodings = ["utf-8-sig", "utf-8", "cp1252"]
    for enc in encodings:
        try:
            df = pd.read_csv(file_path, encoding=enc, dtype=str)
            df.columns = df.columns.str.strip()
            numeric_cols = ['price', 'stars', 'rating', 'num_adults', 'num_children', 'nights', 'rooms_available']
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = df[col].astype(str).str.replace(',', '').str.strip()
                    df[col] = df[col].str.replace(r'\.0$', '', regex=True)
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            return df
        except UnicodeDecodeError:
            continue
        except Exception as e:
            print(f"⚠️ Lỗi khi xử lý file {file_path}: {e}")
            raise
    raise UnicodeDecodeError(f"Không đọc được file {file_path} với UTF-8 hoặc cp1252!")

def yes_no_icon(val):
    return "✅" if str(val).lower() in ("true", "1", "yes") else "❌"

def map_hotel_row(row):
    h = dict(row)
    h["image"] = h.get("image_url", h.get("image", ""))
    html_desc = h.get("review") or h.get("description") or ""
    h["full_desc"] = html_desc
    clean = re.sub(r'<[^>]*>', '', html_desc)
    h["short_desc"] = clean[:150] + ("..." if len(clean) > 150 else "")
    h["gym"] = h.get("gym", False)
    h["spa"] = h.get("spa", False)
    h["sea_view"] = h.get("sea") if "sea" in h else h.get("sea_view", False)
    return h

@app.route('/')
def home():
    hotels_df = read_csv_safe(HOTELS_CSV)
    if 'rooms_available' not in hotels_df.columns:
        hotels_df['rooms_available'] = 0
    hotels_df['rooms_available'] = hotels_df['rooms_available'].astype(int)
    if 'status' not in hotels_df.columns:
        hotels_df['status'] = hotels_df['rooms_available'].apply(lambda x: 'còn' if int(x) > 0 else 'hết')
    cities = sorted(hotels_df['city'].dropna().unique())
    return render_template('index.html', cities=cities)

@app.route('/recommend', methods=['POST', 'GET'])
def recommend():
    filtered = read_csv_safe(HOTELS_CSV)
    if 'rooms_available' not in filtered.columns:
        filtered['rooms_available'] = 0
    filtered['rooms_available'] = filtered['rooms_available'].astype(int)
    if 'status' not in filtered.columns:
        filtered['status'] = filtered['rooms_available'].apply(lambda x: 'còn' if x > 0 else 'hết')
    else:
        filtered['status'] = filtered['rooms_available'].apply(lambda x: 'còn' if x > 0 else 'hết')

    if request.method == 'POST':
        city = request.form.get('location', '').lower()
        budget = request.form.get('budget', '')
        stars = request.form.get('stars', '')
        amenities = request.form.getlist('amenities')
        size = request.form.get('size', '')
    else:
        city = request.args.get('location', '').lower()
        budget = request.args.get('budget', '')
        stars = request.args.get('stars', '')
        amenities = request.args.getlist('amenities')
        size = request.args.get('size', '')

    if city:
        filtered = filtered[filtered['city'].str.lower() == city]

    if budget:
        try:
            budget = float(budget)
            filtered = filtered[filtered['price'] <= budget]
        except Exception:
            pass

    if stars:
        try:
            stars = int(stars)
            filtered = filtered[filtered['stars'] >= stars]
        except Exception:
            pass

    for amen in amenities:
        if amen == 'pool':
            filtered = filtered[filtered['pool'] == True]
        elif amen == 'sea':
            filtered = filtered[(filtered.get('sea', False) == True) | (filtered.get('sea_view', False) == True)]
        elif amen == 'breakfast':
            filtered = filtered[filtered['buffet'] == True]
        elif amen == 'bar':
            filtered = filtered[filtered['bar'] == True]

    if size:
        def room_size_ok(row):
            try:
                s = float(row.get('size', 0))
            except:
                s = 0
            if size == 'small':
                return s < 25
            elif size == 'medium':
                return 25 <= s <= 40
            elif size == 'large':
                return s > 40
            return True
        filtered = filtered[filtered.apply(room_size_ok, axis=1)]

    results = [map_hotel_row(r) for r in filtered.to_dict(orient='records')]
    return render_template('result.html', hotels=results)

@app.route('/hotel/<name>')
def hotel_detail(name):
    hotels_df = read_csv_safe(HOTELS_CSV)
    if 'rooms_available' not in hotels_df.columns:
        hotels_df['rooms_available'] = 0
    hotels_df['rooms_available'] = hotels_df['rooms_available'].astype(int)
    hotels_df['status'] = hotels_df['rooms_available'].apply(lambda x: 'còn' if int(x) > 0 else 'hết')

    hotel_data = hotels_df[hotels_df['name'] == name]

    if hotel_data.empty:
        return "<h3>Không tìm thấy khách sạn!</h3>", 404

    hotel = map_hotel_row(hotel_data.iloc[0].to_dict())
    user_rank = session.get('user', {}).get('rank', 'Đồng')
    reviews_df_local = read_csv_safe(REVIEWS_CSV)
    hotel_reviews = reviews_df_local[reviews_df_local['hotel_name'] == name].to_dict(orient='records')

    avg_rating = (
        round(sum(float(r.get('rating', 0)) for r in hotel_reviews) / len(hotel_reviews), 1)
        if hotel_reviews else hotel.get('rating', 'Chưa có')
    )

    features = {
        "Buffet": yes_no_icon(hotel.get("buffet")),
        "Bể bơi": yes_no_icon(hotel.get("pool")),
        "Gần biển": yes_no_icon(hotel.get("sea_view") or hotel.get("sea")),
        "View biển": yes_no_icon(hotel.get("view")),
    }

    rooms = [
        {
            "type": "Phòng nhỏ",
            "price": get_discounted_price(user_rank, round(float(hotel.get('price', 0)) * 1.0))
        },
        {
            "type": "Phòng đôi",
            "price": get_discounted_price(user_rank, round(float(hotel.get('price', 0)) * 1.5))
        },
        {
            "type": "Phòng tổng thống",
            "price": get_discounted_price(user_rank, round(float(hotel.get('price', 0)) * 2.5))
        },
    ]

    hotel['gallery'] = get_hotel_gallery(hotel['name'])
    hotel['event_image_url'] = hotel_data.iloc[0].get('event_image_url', '')
    if pd.isna(hotel['event_image_url']):
        hotel['event_image_url'] = ''
        
    hotel['hotel_description'] = hotel_data.iloc[0].get('hotel_description', '')
    if pd.isna(hotel['hotel_description']):
        hotel['hotel_description'] = ''

    return render_template(
        'detail.html',
        hotel=hotel,
        features=features,
        rooms=rooms,
        reviews=hotel_reviews,
        avg_rating=avg_rating
    )

@app.route('/review/<name>', methods=['POST'])
def add_review(name):
    user = request.form.get('user', 'Ẩn danh').strip()
    rating = int(request.form.get('rating', 0))
    comment = request.form.get('comment', '').strip()

    new_review = pd.DataFrame([{
        "hotel_name": name,
        "user": user,
        "rating": rating,
        "comment": comment
    }])

    df = read_csv_safe(REVIEWS_CSV)
    df = pd.concat([df, new_review], ignore_index=True)
    df.to_csv(REVIEWS_CSV, index=False, encoding="utf-8-sig")

    return redirect(url_for('hotel_detail', name=name))

# Route đã được sửa lỗi duplicate
@app.route('/booking/<name>/<room_type>', methods=['GET', 'POST'])
def booking(name, room_type):
    hotels_df = read_csv_safe(HOTELS_CSV)
    if 'rooms_available' not in hotels_df.columns:
        hotels_df['rooms_available'] = 0
    hotels_df['rooms_available'] = pd.to_numeric(hotels_df['rooms_available'], errors='coerce').fillna(0).astype(int)
    hotels_df['status'] = hotels_df['rooms_available'].apply(lambda x: 'còn' if x > 0 else 'hết')

    hotel_data = hotels_df[hotels_df['name'] == name]
    if hotel_data.empty:
        return "<h3>Không tìm thấy khách sạn!</h3>", 404

    # Lấy thông tin khách sạn
    hotel = map_hotel_row(hotel_data.iloc[0].to_dict())
    
    # Check số phòng hiện tại
    current_rooms = int(hotel_data.iloc[0]['rooms_available'])
    hotel['status'] = 'còn' if current_rooms > 0 else 'hết'
    is_available = current_rooms > 0
    
    if not is_available:
        flash(f"Rất tiếc, khách sạn này đã hết phòng!", "danger")
    else:
        flash(f"Trạng thái phòng hiện tại: Còn {current_rooms} phòng", "info")

    user_rank = session.get('user', {}).get('rank', 'Đồng')
    base_price = float(hotel.get('price', 0))
    discounted_price = get_discounted_price(user_rank, base_price)

    if request.method == 'POST':
        if current_rooms <= 0:
            flash("Xin lỗi, phòng vừa mới hết!", "danger")
            return redirect(url_for('hotel_detail', name=name))

        username = session.get('user', {}).get('username', 'Khách vãng lai')
        email = request.form.get('email', '').strip()
        fullname = request.form['fullname'].strip()
        phone = request.form['phone'].strip()
        num_adults = max(int(request.form.get('adults', 1)), 1)
        num_children = max(int(request.form.get('children', 0)), 0)
        checkin = request.form['checkin']
        note = request.form.get('note', '').strip()

        info = {
            "username": username,
            "hotel_name": name,
            "room_type": room_type,
            "price": float(request.form.get('price', discounted_price)),
            "user_name": fullname,
            "phone": phone,
            "email": email,
            "num_adults": num_adults,
            "num_children": num_children,
            "checkin_date": checkin,
            "nights": 1,
            "special_requests": note,
            "booking_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "Chờ xác nhận",
            "booking_code": generate_booking_code()
        }

        try:
            bookings_df = pd.read_csv(BOOKINGS_CSV, encoding="utf-8-sig")
        except FileNotFoundError:
            bookings_df = pd.DataFrame(columns=info.keys())
        
        bookings_df = pd.concat([bookings_df, pd.DataFrame([info])], ignore_index=True)
        bookings_df.to_csv(BOOKINGS_CSV, index=False, encoding="utf-8-sig")

        # CẬP NHẬT SỐ PHÒNG TRONG HOTELS.CSV
        hotel_idx = hotels_df.index[hotels_df['name'] == name].tolist()
        if hotel_idx:
            idx = hotel_idx[0]
            new_room_count = max(0, current_rooms - 1)
            hotels_df.at[idx, 'rooms_available'] = new_room_count
            if new_room_count == 0:
                hotels_df.at[idx, 'status'] = 'hết'
            hotels_df.to_csv(HOTELS_CSV, index=False, encoding="utf-8-sig")

        if "user" in session:
            if username in users_db:
                users_db[username]['total_spent'] += info['price']
                save_users(users_db)
                session['user']['rank'] = get_user_rank(users_db[username]['total_spent'])

        if email:
            try:
                msg_user = Message(subject="Xác nhận đặt phòng - Hotel Pinder", recipients=[email])
                msg_user.html = render_template("msg_user.html", info=info)
                mail.send(msg_user)
            except Exception as e:
                print(f"Lỗi gửi email cho khách: {e}")

        try:
            msg_admin = Message(subject=f"Đơn đặt phòng mới tại {info['hotel_name']}", recipients=["hotelpinder@gmail.com"])
            msg_admin.html = f"""
                <h3>Đơn đặt phòng mới</h3>
                <p>Khách sạn: {info['hotel_name']}</p>
                <p>Người đặt: {info['user_name']}</p>
                <p>Mã đặt phòng: {info['booking_code']}</p>
            """
            mail.send(msg_admin)
        except Exception as e:
            print(f"Lỗi gửi email admin: {e}")

        flash("Đặt phòng thành công!", "success")
        return render_template('success.html', info=info)

    return render_template('booking.html', hotel=hotel, room_type=room_type, 
                           is_available=is_available, discounted_price=discounted_price)

@app.route("/history")
def booking_history():
    user = session.get("user")
    if not user:
        flash("Bạn cần đăng nhập để xem lịch sử.", "danger")
        return redirect(url_for("login"))

    is_admin = user.get("rank", "").lower() == "admin"
    email = request.args.get("email") if is_admin else user["email"]

    try:
        df = pd.read_csv(BOOKINGS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        df = pd.DataFrame()
    
    bookings = df[df['email'] == email].to_dict(orient="records") if not df.empty else []
    return render_template("history.html", bookings=bookings, email=email, is_admin=is_admin, user=user)

@app.route('/about')
def about_page():
    return render_template('about.html')

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if username == "admin" and password == "123456":
            session['admin'] = True
            flash("Đăng nhập admin thành công!", "success")
            return redirect(url_for('admin_dashboard'))
        else:
            flash("Sai tài khoản hoặc mật khẩu!", "danger")
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    flash("Đã đăng xuất!", "info")
    return redirect(url_for('admin_login'))

@app.route('/admin')
def admin_dashboard():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))

    hotels_df = pd.read_csv(HOTELS_CSV, encoding='utf-8-sig')
    bookings_df = pd.read_csv(BOOKINGS_CSV, encoding='utf-8-sig') if os.path.exists(BOOKINGS_CSV) else pd.DataFrame()

    total_hotels = len(hotels_df)
    total_bookings = len(bookings_df)
    total_cities = hotels_df['city'].nunique()

    return render_template('admin_dashboard.html',
                           total_hotels=total_hotels,
                           total_bookings=total_bookings,
                           total_cities=total_cities)

@app.route('/admin/hotels', methods=['GET', 'POST'])
def admin_hotels():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))

    df = pd.read_csv(HOTELS_CSV, encoding='utf-8-sig')

    if 'rooms_available' not in df.columns:
        df['rooms_available'] = 1
    if 'status' not in df.columns:
        df['status'] = 'còn'

    df['rooms_available'] = df['rooms_available'].astype(str).str.replace(',', '').str.strip()
    df['rooms_available'] = df['rooms_available'].str.replace(r'\.0$', '', regex=True)
    df['rooms_available'] = pd.to_numeric(df['rooms_available'], errors='coerce').fillna(0).astype(int)
    df['status'] = df['rooms_available'].apply(lambda x: 'còn' if x > 0 else 'hết')
    df.to_csv(HOTELS_CSV, index=False, encoding='utf-8-sig')

    if request.method == 'POST' and 'name' in request.form and 'add_hotel' not in request.form:
        name = request.form.get('name', '').strip()
        city = request.form.get('city', '').strip()
        price = request.form.get('price', '').strip()
        stars = request.form.get('stars', '').strip()
        description = request.form.get('description', '').strip()
        rooms_available = request.form.get('rooms_available', 1)

        try:
            rooms_available = int(float(str(rooms_available).replace(',', '').replace('.0', '')))
        except Exception:
            rooms_available = 1

        if name and city:
            new_row = {
                "name": name,
                "city": city,
                "price": price,
                "stars": stars,
                "description": description,
                "rooms_available": rooms_available,
                "status": "còn" if rooms_available > 0 else "hết"
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            df.to_csv(HOTELS_CSV, index=False, encoding='utf-8-sig')
            flash("✅ Đã thêm khách sạn mới!", "success")
            return redirect(url_for('admin_hotels'))
        else:
            flash("⚠️ Tên và thành phố không được để trống!", "warning")

    if request.method == 'POST' and 'update_hotel' in request.form:
        update_name = request.form.get('update_name', '').strip()
        update_rooms = request.form.get('update_rooms', '').strip()

        try:
            update_rooms = int(float(str(update_rooms).replace(',', '').replace('.0', '')))
        except ValueError:
            update_rooms = 0

        if update_name in df['name'].values:
            df.loc[df['name'] == update_name, 'rooms_available'] = update_rooms
            df.loc[df['name'] == update_name, 'status'] = 'còn' if update_rooms > 0 else 'hết'
            df.to_csv(HOTELS_CSV, index=False, encoding='utf-8-sig')
            flash(f"🔧 Đã cập nhật số phòng cho {update_name}", "success")
        else:
            flash("⚠️ Không tìm thấy khách sạn có tên này!", "danger")

    hotels = df.to_dict(orient='records')
    return render_template('admin_hotels.html', hotels=hotels)

@app.route('/admin/bookings')
def admin_bookings():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    if os.path.exists(BOOKINGS_CSV):
        df = pd.read_csv(BOOKINGS_CSV, encoding='utf-8-sig')
        bookings = df.to_dict(orient='records')
    else:
        bookings = []
    return render_template('admin_bookings.html', bookings=bookings)

@app.route('/admin/bookings/confirm/<booking_time>')
def admin_confirm_booking(booking_time):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    df = pd.read_csv(BOOKINGS_CSV, encoding='utf-8-sig')
    df.loc[df['booking_time'] == booking_time, 'status'] = 'Đã xác nhận'
    df.to_csv(BOOKINGS_CSV, index=False, encoding='utf-8-sig')
    flash("Đã xác nhận đặt phòng!", "success")
    return redirect(url_for('admin_bookings'))

@app.route('/admin/bookings/delete/<booking_time>')
def admin_delete_booking(booking_time):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    df = pd.read_csv(BOOKINGS_CSV, encoding='utf-8-sig')
    df = df[df['booking_time'] != booking_time]
    df.to_csv(BOOKINGS_CSV, index=False, encoding='utf-8-sig')
    flash("Đã xóa đặt phòng!", "info")
    return redirect(url_for('admin_bookings'))

@app.route('/admin/hotels/delete/<name>')
def delete_hotel(name):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    try:
        df = pd.read_csv(HOTELS_CSV, encoding='utf-8-sig')
        df = df[df['name'] != name]
        df.to_csv(HOTELS_CSV, index=False, encoding='utf-8-sig')
        flash(f"Đã xóa khách sạn: {name}", "info")
    except Exception as e:
        flash(f"Lỗi khi xóa khách sạn: {e}", "danger")
    return redirect(url_for('admin_hotels'))

@app.route('/admin/hotels/status/<name>/<status>')
def update_hotel_status(name, status):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    try:
        df = pd.read_csv(HOTELS_CSV, encoding='utf-8-sig')
        if name in df['name'].values:
            df.loc[df['name'] == name, 'status'] = status
            if status.strip().lower() == 'còn':
                df.loc[df['name'] == name, 'rooms_available'] = df.loc[df['name'] == name, 'rooms_available'].replace(0, 1)
            elif status.strip().lower() == 'hết':
                df.loc[df['name'] == name, 'rooms_available'] = 0
            df['status'] = df['rooms_available'].apply(lambda x: 'còn' if x > 0 else 'hết')
            df.to_csv(HOTELS_CSV, index=False, encoding='utf-8-sig')
            flash(f"✅ Đã cập nhật {name} → {status}", "success")
        else:
            flash("⚠️ Không tìm thấy khách sạn này!", "warning")
    except Exception as e:
        flash(f"Lỗi khi cập nhật trạng thái: {e}", "danger")
    return redirect(url_for('admin_hotels'))

# ------------------------
# GEMINI API
# ------------------------
try:
    GEMINI_API_KEY = os.environ.get("GOOGLE_API_KEY", "DÁN_GEMINI_API_KEY_CỦA_ANH_VÀO_ĐÂY")
    if not GEMINI_API_KEY or GEMINI_API_KEY == "DÁN_GEMINI_API_KEY_CỦA_ANH_VÀO_ĐÂY":
        print("CẢNH BÁO: GOOGLE_API_KEY chưa được set.")
    
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
except Exception as e:
    print(f"Lỗi khởi tạo Gemini: {e}")
    model = None

@app.route('/ai_chat')
def ai_chat():
    return render_template('ai_chat_hotel.html')

@app.route('/api/chat', methods=['POST'])
def api_chat():
    if not model:
        return jsonify({"error": "Gemini AI chưa được cấu hình"}), 500
        
    try:
        user_query = request.json.get('query')
        include_hotels = request.json.get('include_hotels', True)
        conversation_history = request.json.get('history', [])
        
        if not user_query:
            return jsonify({"error": "Missing query"}), 400

        hotels_data = []
        reviews_data = []
        events_data = []
        
        try:
            hotels_df = pd.read_csv("hotels.csv", encoding='utf-8-sig')
            for _, hotel in hotels_df.iterrows():
                hotel_info = {
                    'name': hotel.get('name', ''),
                    'city': hotel.get('city', ''),
                    'district': hotel.get('district', 'Trung tâm'),
                    'price': hotel.get('price', 'Liên hệ'),
                    'rating': hotel.get('rating', 4.0),
                    'amenities': hotel.get('amenities', 'WiFi, Restaurant, Pool'),
                    'description': hotel.get('description', 'Khách sạn chất lượng với đầy đủ tiện ích')
                }
                hotels_data.append(hotel_info)
            
            reviews_df = pd.read_csv("reviews.csv", encoding='utf-8-sig')
            for _, review in reviews_df.iterrows():
                review_info = {
                    'hotel_name': review.get('hotel_name', ''),
                    'user': review.get('user', 'Khách hàng'),
                    'rating': review.get('rating', 4.5),
                    'comment': review.get('comment', 'Trải nghiệm tuyệt vời!')
                }
                reviews_data.append(review_info)
            
            if os.path.exists("events.csv"):
                events_df = pd.read_csv("events.csv", encoding='utf-8-sig')
                for _, event in events_df.iterrows():
                    event_info = {
                        'event_name': event.get('event_name', ''),
                        'city': event.get('city', ''),
                        'start_date': event.get('start_date', ''),
                        'end_date': event.get('end_date', ''),
                        'season': event.get('season', 'Không xác định'),
                        'description': event.get('description', ''),
                        'best_time': event.get('best_time', ''),
                        'weather': event.get('weather', '')
                    }
                    events_data.append(event_info)
                
        except Exception as e:
            print(f"Lỗi đọc CSV: {e}")
            hotels_data = [{
                'name': 'Sunrise Nha Trang',
                'city': 'Nha Trang',
                'district': 'Trần Phú',
                'price': '2,500,000 VNĐ',
                'rating': 4.8,
                'amenities': 'Pool, Spa, Beach Front, Restaurant, Bar',
                'description': 'Khách sạn 5 sao view biển tuyệt đẹp với hồ bơi vô cực'
            }]
            events_data = []

        query_analysis = analyze_user_query(user_query, conversation_history)
        need_hotel_recommendation = query_analysis['need_hotel_recommendation']
        should_show_cards = query_analysis['should_show_cards']
        is_greeting = query_analysis['is_greeting']
        
        hotel_names_list = [hotel['name'] for hotel in hotels_data]
        city_events_info = build_city_events_info(events_data)
        context_info = build_conversation_context(conversation_history)
        
        system_prompt = f"""
Bạn là trợ lý du lịch THÔNG MINH, CHUYÊN NGHIỆP. Hãy phân tích và trả lời câu hỏi MỘT CÁCH PHÙ HỢP.

{context_info}

THÔNG TIN DU LỊCH THEO THÀNH PHỐ (dùng để tư vấn):
{city_events_info}

DANH SÁCH KHÁCH SẠN THỰC TẾ (CHỈ ĐƯỢC ĐỀ XUẤT NHỮNG KHÁCH SẠN NÀY):
{', '.join(hotel_names_list)}

QUY TẮC QUAN TRỌNG:
1. CHỈ đề xuất khách sạn từ danh sách trên
2. KHÔNG tạo ra khách sạn không tồn tại
3. Nếu không có khách sạn phù hợp, đề xuất tiêu chí khác

CÁCH TRẢ LỜI:
- {"" if is_greeting else "KHÔNG chào lại nếu đã trong cuộc trò chuyện"}
- Tự nhiên, ngắn gọn, đúng trọng tâm
- Hiểu các từ viết tắt: "ks" = khách sạn, "biet" = biết, "ko" = không, "dc" = được
- Khi được hỏi "bạn biết khách sạn X không" → kiểm tra trong danh sách và trả lời CÓ/KHÔNG kèm thông tin nếu có

KHI ĐỀ XUẤT KHÁCH SẠN:
- Chọn 1-3 khách sạn phù hợp nhất
- Mô tả ngắn: vị trí, giá, tiện ích nổi bật
- Kết thúc bằng: "Đây là những khách sạn phù hợp từ hệ thống!"
"""

        max_retries = 2
        for attempt in range(max_retries):
            try:
                full_prompt = system_prompt + f"\n\nCâu hỏi: {user_query}"
                response = model.generate_content(
                    full_prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.3,
                        max_output_tokens=1500
                    )
                )
                ai_response = response.text
                cleaned_response = clean_ai_response(ai_response, is_greeting, conversation_history)
                response_data = {"response": cleaned_response}
                
                if should_show_cards and include_hotels and need_hotel_recommendation:
                    recommended_hotels = get_recommended_hotels_from_ai_response(
                        hotels_data, reviews_data, user_query, cleaned_response, query_analysis
                    )
                    response_data["hotels"] = recommended_hotels[:3]
                
                return jsonify(response_data)
                
            except Exception as e:
                if "quota" in str(e).lower() or "429" in str(e):
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        return jsonify({"error": "Hệ thống đang quá tải. Vui lòng thử lại sau 1 phút."}), 429
                else:
                    raise e

        return jsonify({"error": "Lỗi kết nối. Vui lòng thử lại."}), 500

    except Exception as e:
        print(f"Lỗi API chat: {e}")
        return jsonify({"response": "Hiện tại hệ thống đang gặp sự cố kỹ thuật. Tôi vẫn muốn lắng nghe và hỗ trợ bạn. Hãy thử lại sau ít phút nhé!"})

def analyze_user_query(user_query, conversation_history):
    query_lower = user_query.lower()
    normalized_query = normalize_vietnamese_slang(query_lower)
    is_greeting = any(word in normalized_query for word in [
        'chào', 'hello', 'hi', 'xin chào', 'hey'
    ]) and len(conversation_history) == 0
    is_specific_hotel_inquiry = any(pattern in normalized_query for pattern in [
        'bạn biết khách sạn', 'bạn biết ks', 'bạn có biết khách sạn', 
        'bạn có biết ks', 'khách sạn này', 'ks này'
    ])
    need_hotel_recommendation = any(keyword in normalized_query for keyword in [
        'tìm khách sạn', 'đề xuất khách sạn', 'khách sạn nào', 'ở đâu',
        'tìm chỗ ở', 'booking', 'đặt phòng', 'recommend', 'suggest', 'hotel',
        'nghỉ ở đâu', 'chỗ ở', 'khách sạn', 'resort', 'nhà nghỉ', 'tư vấn khách sạn',
        'nên ở đâu', 'ở khách sạn nào'
    ]) and not is_specific_hotel_inquiry
    should_show_cards = need_hotel_recommendation and not is_specific_hotel_inquiry
    
    return {
        'is_greeting': is_greeting,
        'need_hotel_recommendation': need_hotel_recommendation,
        'should_show_cards': should_show_cards,
        'normalized_query': normalized_query,
        'is_specific_hotel_inquiry': is_specific_hotel_inquiry
    }

def normalize_vietnamese_slang(text):
    replacements = {
        ' ks ': ' khách sạn ',
        ' ko ': ' không ',
        ' dc ': ' được ',
        ' bt ': ' biết ',
        ' bik ': ' biết ',
        ' biet ': ' biết ',
        ' ng ': ' người ',
        ' tk ': ' tìm kiếm ',
        ' dl ': ' du lịch ',
    }
    normalized = text
    for short, full in replacements.items():
        normalized = normalized.replace(short, full)
    return normalized

def build_city_events_info(events_data):
    if not events_data:
        return "Hiện chưa có thông tin sự kiện."
    city_events = {}
    for event in events_data:
        city = event.get('city', '')
        if city not in city_events:
            city_events[city] = []
        event_info = f"- {event.get('event_name', '')}"
        if event.get('season'):
            event_info += f" (Mùa: {event.get('season')})"
        if event.get('best_time'):
            event_info += f" - Thời gian tốt: {event.get('best_time')}"
        city_events[city].append(event_info)
    result = []
    for city, events in city_events.items():
        result.append(f"{city}:")
        result.extend(events)
    return "\n".join(result) if result else "Hiện chưa có thông tin sự kiện."

def build_conversation_context(conversation_history):
    if not conversation_history:
        return "Đây là tin nhắn đầu tiên."
    recent_history = conversation_history[-4:]
    context_lines = ["Lịch sử trò chuyện gần đây:"]
    for msg in recent_history:
        role = "User" if msg.get('role') == 'user' else "Assistant"
        content = msg.get('content', '')[:100]
        context_lines.append(f"{role}: {content}")
    context_lines.append("\nHãy tiếp tục cuộc trò chuyện một cách tự nhiên.")
    return "\n".join(context_lines)

def clean_ai_response(ai_response, is_greeting, conversation_history):
    cleaned = ai_response.replace('**', '').replace('*', '').strip()
    if not is_greeting and len(conversation_history) > 0:
        greeting_patterns = [
            'xin chào', 'chào bạn', 'chào mừng', 'hello', 'hi ',
            'rất vui được gặp bạn', 'chào anh', 'chào chị'
        ]
        for pattern in greeting_patterns:
            if cleaned.lower().startswith(pattern):
                sentences = cleaned.split('.')
                if len(sentences) > 1:
                    cleaned = '.'.join(sentences[1:]).strip()
                    if cleaned.startswith(','):
                        cleaned = cleaned[1:].strip()
                break
    return cleaned

def get_recommended_hotels_from_ai_response(hotels_data, reviews_data, user_query, ai_response, query_analysis):
    if query_analysis.get('is_specific_hotel_inquiry', False):
        return []
    
    target_city = extract_city_from_query(query_analysis.get('normalized_query', user_query.lower()))
    if not target_city:
        target_city = extract_city_from_query(ai_response.lower())
    
    mentioned_hotels = []
    ai_response_lower = ai_response.lower()
    
    for hotel in hotels_data:
        hotel_name = hotel['name']
        hotel_city = hotel.get('city', '').lower().strip()
        if target_city and hotel_city != target_city.lower():
            continue
        
        if hotel_name.lower() in ai_response_lower:
            hotel_reviews = [r for r in reviews_data if r['hotel_name'] == hotel_name]
            if hotel_reviews:
                hotel['review'] = hotel_reviews[0]
            mentioned_hotels.append(hotel)
    
    if mentioned_hotels:
        return mentioned_hotels[:3]
    
    if not target_city:
        if 'nha trang' in user_query.lower() or 'nha trang' in ai_response.lower():
            target_city = 'Nha Trang'
        elif 'hồ chí minh' in user_query.lower() or 'hồ chí minh' in ai_response.lower():
            target_city = 'Hồ Chí Minh'
        elif 'hà nội' in user_query.lower() or 'hà nội' in ai_response.lower():
            target_city = 'Hà Nội'
        elif 'đà nẵng' in user_query.lower() or 'đà nẵng' in ai_response.lower():
            target_city = 'Đà Nẵng'
    
    filtered_hotels = smart_hotel_filtering_with_city_constraint(hotels_data, reviews_data, user_query, query_analysis, target_city)
    
    if filtered_hotels and should_show_hotel_cards(ai_response, filtered_hotels, target_city):
        return filtered_hotels[:3]
    return []

def smart_hotel_filtering_with_city_constraint(hotels_data, reviews_data, user_query, query_analysis, target_city):
    query_lower = query_analysis.get('normalized_query', user_query.lower())
    scored_hotels = []
    
    budget_range = extract_budget_from_query(query_lower)
    amenities_needed = extract_amenities_from_query(query_lower)
    hotel_type = extract_hotel_type_from_query(query_lower)
    
    for hotel in hotels_data:
        hotel_city = hotel.get('city', '').strip()
        hotel_city_normalized = normalize_city_name(hotel_city)
        target_city_normalized = normalize_city_name(target_city) if target_city else ""
        
        if target_city and hotel_city_normalized != target_city_normalized:
            continue
        
        score = 10
        if budget_range:
            hotel_price = extract_price_value(hotel.get('price', ''))
            if hotel_price and budget_range[0] <= hotel_price <= budget_range[1]:
                score += 8
        
        if amenities_needed:
            hotel_amenities = hotel.get('amenities', '').lower()
            for amenity in amenities_needed:
                if amenity in hotel_amenities:
                    score += 3
        
        hotel_rating = hotel.get('rating', 0)
        if hotel_type == 'luxury' and hotel_rating >= 4.5:
            score += 10
        
        score += hotel_rating * 0.5
        hotel['match_score'] = score
        scored_hotels.append(hotel)
    
    scored_hotels.sort(key=lambda x: x.get('match_score', 0), reverse=True)
    return scored_hotels[:3] if scored_hotels else []

def should_show_hotel_cards(ai_response, filtered_hotels, target_city):
    ai_lower = ai_response.lower()
    denial_phrases = ['không tìm thấy', 'không có', 'chưa có']
    if any(phrase in ai_lower for phrase in denial_phrases):
        return False
    hotel_mention_phrases = ['khách sạn', 'resort', 'hotel', 'đề xuất', 'gợi ý']
    return any(phrase in ai_lower for phrase in hotel_mention_phrases)

def normalize_city_name(city_name):
    if not city_name: return ""
    city_mapping = {
        'hà nội': 'Hanoi', 'hanoi': 'Hanoi',
        'đà nẵng': 'Da Nang', 'danang': 'Da Nang', 
        'nha trang': 'Nha Trang', 'nhatrang': 'Nha Trang',
        'hồ chí minh': 'Ho Chi Minh', 'ho chi minh': 'Ho Chi Minh',
        'sài gòn': 'Ho Chi Minh'
    }
    return city_mapping.get(city_name.lower().strip(), city_name)

def extract_city_from_query(query):
    city_mapping = {
        'hà nội': 'Hanoi', 'hanoi': 'Hanoi',
        'đà nẵng': 'Da Nang', 'danang': 'Da Nang',
        'nha trang': 'Nha Trang', 'nhatrang': 'Nha Trang',
        'hồ chí minh': 'Ho Chi Minh', 'sài gòn': 'Ho Chi Minh', 'hcm': 'Ho Chi Minh'
    }
    for keyword, val in city_mapping.items():
        if keyword in query.lower():
            return val
    return None

def extract_budget_from_query(query):
    if 'triệu' in query or 'million' in query:
        if 'dưới 1' in query: return (500000, 2000000)
        elif '2-3' in query: return (2000000, 3000000)
    return (1000000, 5000000)

def extract_amenities_from_query(query):
    amenities = []
    amenity_mapping = {
        'hồ bơi': 'pool', 'pool': 'pool',
        'spa': 'spa', 'gym': 'gym',
        'nhà hàng': 'restaurant', 'biển': 'beach'
    }
    for keyword, amenity in amenity_mapping.items():
        if keyword in query:
            amenities.append(amenity)
    return list(set(amenities))

def extract_hotel_type_from_query(query):
    if any(w in query for w in ['sang trọng', 'luxury', '5 sao']): return 'luxury'
    if any(w in query for w in ['bình dân', 'budget', 'giá rẻ']): return 'budget'
    return 'midrange'

def extract_price_value(price_str):
    try:
        clean = re.sub(r'[^\d]', '', str(price_str))
        return int(clean) if clean else None
    except:
        return None

def google_search(query):
    try:
        search_url = f"https://www.google.com/search?q={requests.utils.quote(query + ' site:việt nam')}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        requests.get(search_url, headers=headers, timeout=10)
        return f"Đã tìm thấy thông tin về: {query}"
    except Exception as e:
        return f"Không thể tìm kiếm thông tin: {str(e)}"

# -------------------------
# ROUTES SỰ KIỆN
# -------------------------
@app.route('/event/user-info')
def event_user_info():
    if 'user' not in session: return jsonify({'error': 'Unauthorized'}), 401
    username = session['user']['username']
    total_spent = calculate_event_spending(username)
    spin_info = get_max_spins(username)
    used_spins = get_used_spins(username)
    
    event_bookings = []
    if os.path.exists(BOOKINGS_CSV):
        with open(BOOKINGS_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['username'] == username and row['status'] == 'completed':
                    event_bookings.append({'hotel': row['hotel_name']})
    
    return jsonify({
        'username': username,
        'rank': spin_info['rank'],
        'total_spent': total_spent,
        'spins_remaining': max(0, spin_info['total_spins'] - used_spins),
        'event_bookings': event_bookings
    })

@app.route('/event')
def event_page():
    return render_template('event.html')

@app.route('/event/check-eligibility')
def check_eligibility():
    if 'user' not in session: return jsonify({'eligible': False})
    current_month = datetime.now().month
    if current_month < EVENT_CONFIG['start_month'] or current_month > EVENT_CONFIG['end_month']:
        return jsonify({'eligible': False, 'message': 'Sự kiện chưa diễn ra'})
    
    username = session['user']['username']
    spin_info = get_max_spins(username)
    used_spins = get_used_spins(username)
    spins_remaining = max(0, spin_info['total_spins'] - used_spins)
    
    return jsonify({
        'eligible': spins_remaining > 0,
        'spins_remaining': spins_remaining,
        'username': username
    })

@app.route('/event/spin-wheel', methods=['POST'])
def spin_wheel():
    if 'user' not in session: return jsonify({'error': 'Unauthorized'}), 401
    username = session['user']['username']
    if not use_spin(username):
        return jsonify({'error': 'Hết lượt quay'}), 400
    
    prize = get_random_prize()
    if prize['value'] > 0:
        update_user_prize(username, prize['value'], prize['name'])
    
    prize_index = next(i for i, p in enumerate(EVENT_CONFIG['prizes']) if p['value'] == prize['value'])
    sector_angle = 360 / len(EVENT_CONFIG['prizes'])
    final_angle = 360 - (prize_index * sector_angle + random.uniform(sector_angle * 0.1, sector_angle * 0.9))
    
    spin_info = get_max_spins(username)
    used_spins = get_used_spins(username)
    
    return jsonify({
        'prize_name': prize['name'],
        'prize_value': prize['value'],
        'final_angle': final_angle,
        'spins_remaining': max(0, spin_info['total_spins'] - used_spins)
    })

if __name__ == '__main__':
    app.run(debug=True)

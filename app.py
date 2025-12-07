import os
import re
import ast
import tempfile
import random
import time
import csv
from datetime import datetime
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_mail import Mail, Message   # nếu dùng mail
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
    # THÊM: Số lượt quay thêm theo rank
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
# USER DATABASE (tạm thời dict)
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

# -------------------------
# HÀM HỖ TRỢ SỰ KIỆN VÒNG QUAY
# -------------------------
def init_event_files():
    """Khởi tạo file CSV cho sự kiện nếu chưa tồn tại"""
    if not os.path.exists(EVENT_SPINS_CSV):
        with open(EVENT_SPINS_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['username', 'spin_date', 'year', 'is_free_spin'])
    
    if not os.path.exists(EVENT_PRIZES_CSV):
        with open(EVENT_PRIZES_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['username', 'prize_value', 'prize_name', 'created_at'])

def user_exists_in_bookings(username):
    """Kiểm tra user có tồn tại trong bookings.csv không"""
    if not os.path.exists(BOOKINGS_CSV):
        return False
    
    with open(BOOKINGS_CSV, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['username'] == username:
                return True
    return False

def calculate_event_spending(username):
    """Tính tổng chi tiêu TRONG THỜI GIAN SỰ KIỆN từ bookings.csv - ĐÃ SỬA"""
    total = 0
    
    if not os.path.exists(BOOKINGS_CSV):
        return total
    
    current_year = datetime.now().year
    
    with open(BOOKINGS_CSV, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row['username'] == username and 
                row['status'].lower() == 'completed'):
                
                try:
                    booking_time = datetime.strptime(row['booking_time'], '%Y-%m-%d %H:%M:%S')
                    # CHỈ tính booking trong thời gian sự kiện (tháng 8-12)
                    if (booking_time.year == current_year and 
                        EVENT_CONFIG['start_month'] <= booking_time.month <= EVENT_CONFIG['end_month']):
                        total += float(row['price'])
                except (ValueError, KeyError):
                    continue
    
    # ❌ KHÔNG cộng thêm giải thưởng từ sự kiện nữa
    # Vì giải thưởng đã được cộng trực tiếp vào total_spent của user
    
    return total

def get_max_spins(username):
    """Tính tổng số lượt quay tối đa = 1 lượt miễn phí + lượt từ chi tiêu + lượt từ rank"""
    # Lấy total_spent từ users_db (đã bao gồm giải thưởng)
    user_data = users_db.get(username, {})
    total_spent = user_data.get('total_spent', 0)  # ✅ Đã có giải thưởng
    rank = get_user_rank(total_spent)
    
    # 1 lượt MIỄN PHÍ ban đầu cho mỗi tài khoản
    free_spin = 1
    
    # Tính lượt từ chi tiêu (dùng total_spent đã có giải thưởng)
    spend_spins = 0
    for threshold in EVENT_CONFIG['spend_thresholds']:
        if total_spent >= threshold:
            spend_spins += 1
    
    # Tính lượt từ rank
    rank_bonus = EVENT_CONFIG['rank_bonus_spins'].get(rank, 0)
    
    # Tổng lượt quay
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
    """Đếm số lượt quay đã sử dụng"""
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
    """Ghi nhận một lượt quay - FIXED"""
    current_year = datetime.now().year
    current_month = datetime.now().month
    
    # Kiểm tra thời gian sự kiện
    if not (EVENT_CONFIG['start_month'] <= current_month <= EVENT_CONFIG['end_month']):
        print(f"❌ Không trong thời gian sự kiện: tháng {current_month}")
        return False
    
    # FIX: Bỏ điều kiện user phải có booking
    # Mỗi user đều có 1 lượt miễn phí, không cần booking
    
    # Tính lượt quay còn lại
    spin_info = get_max_spins(username)
    used_spins = get_used_spins(username)
    
    print(f"📊 User {username}: total={spin_info['total_spins']}, used={used_spins}")
    
    if used_spins >= spin_info['total_spins']:
        print(f"❌ {username} đã hết lượt quay")
        return False
    
    # Kiểm tra xem đây có phải là lượt miễn phí đầu tiên không
    is_free_spin = (used_spins == 0)
    
    # Ghi lượt quay
    with open(EVENT_SPINS_CSV, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([username, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), current_year, is_free_spin])
    
    print(f"✅ Đã ghi lượt quay cho {username}, free_spin={is_free_spin}")
    return True

def get_random_prize():
    """Lấy giải thưởng ngẫu nhiên dựa trên xác suất"""
    prizes = []
    for prize in EVENT_CONFIG['prizes']:
        prizes.extend([prize] * prize['probability'])
    
    return random.choice(prizes)

def update_user_prize(username, prize_value, prize_name):
    """Cập nhật giải thưởng cho user - CHỈ cộng vào total_spent"""
    # 1. Ghi giải thưởng vào event_prizes.csv
    with open(EVENT_PRIZES_CSV, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([username, prize_value, prize_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S')])
    
    # 2. Cập nhật tổng chi tiêu trong users_db (CHÍNH)
    if username in users_db:
        users_db[username]['total_spent'] += prize_value
        save_users(users_db)  # Lưu ngay vào CSV
        
        print(f"✅ Đã cộng {prize_value:,} VNĐ vào total_spent của user {username}")
        print(f"💰 Total_spent mới: {users_db[username]['total_spent']:,} VNĐ")
    
    # 3. KHÔNG thêm booking giả nữa (đã xóa add_prize_to_booking_csv)

def generate_booking_code():
    return str(random.randint(10000000, 99999999))
# -------------------------
# HỖ TRỢ USER CSV
# -------------------------
def load_users():
    # Nếu file chưa tồn tại, tạo DataFrame rỗng với header chuẩn
    if not os.path.exists(USERS_CSV):
        df = pd.DataFrame(columns=[
            "username","password","full_name","dob","gender","email","phone","total_spent","history"
        ])
        df.to_csv(USERS_CSV, index=False, encoding="utf-8-sig")
    else:
        df = pd.read_csv(USERS_CSV, encoding="utf-8-sig")
        # Nếu không có cột username, tạo DataFrame rỗng
        if "username" not in df.columns:
            df = pd.DataFrame(columns=[
                "username","password","full_name","dob","gender","email","phone","total_spent","history"
            ])
            df.to_csv(USERS_CSV, index=False, encoding="utf-8-sig")

    # Chuyển DataFrame thành dict theo username
    users = df.set_index('username').T.to_dict()

    # 🔹 Chuyển 'history' từ string -> list
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
    # 🔹 Chuyển 'history' từ list -> string trước khi lưu CSV
    df['history'] = df['history'].apply(str)
    df.to_csv(USERS_CSV, index_label='username', encoding="utf-8-sig")

# Load user database khi start app
users_db = load_users()

# -------------------------
# ROUTES
# -------------------------

# Trang chủ + danh sách khách sạn
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

# Đăng ký
@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        if username in users_db:
            flash("Tài khoản đã tồn tại!", "danger")
            return redirect(url_for("register"))

        # Thêm user vào dict
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

        # Ghi lại CSV
        df = pd.DataFrame(users_db).T  # Chuyển dict sang DataFrame
        df.to_csv(USERS_CSV, index_label="username", encoding="utf-8-sig")

        flash("Đăng ký thành công! Hãy đăng nhập.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")

# Đăng nhập
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

# Đăng xuất
@app.route("/logout")
def logout():
    session.clear()
    flash("Đã đăng xuất!", "success")
    return redirect(url_for("index"))

# Trang cá nhân
@app.route("/profile")
def profile():
    if "user" not in session:
        flash("Bạn cần đăng nhập để xem thông tin.", "danger")
        return redirect(url_for("login"))

    user_session = session["user"]
    username = user_session["username"]
    user_data = users_db.get(username, {})

    # Tính tuổi
    dob = user_data.get("dob", "")
    age = "-"
    if dob:
        birth = datetime.strptime(dob, "%Y-%m-%d")
        age = int((datetime.now() - birth).days / 365.25)

    # --- Lấy lịch sử đặt phòng ---
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

    # --- Truyền total_spent vào template ---
    total_spent = user_data.get("total_spent", 0)

    return render_template(
        "profile.html",
        user=user_data,
        age=age,
        user_rank=user_session.get("rank", "Đồng"),
        total_spent=total_spent,
        history=history
    )

# Đặt phòng
@app.route("/book/<hotel_name>/<int:price>", methods=["POST"])
def book(hotel_name, price):
    if "user" not in session:
        flash("Bạn cần đăng nhập để đặt phòng.", "danger")
        return redirect(url_for("login"))

    username = session["user"]
    users_db[username]["total_spent"] += price
    users_db[username]["history"].append({
        "name": hotel_name,
        "price": price,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    session["user_rank"] = get_user_rank(users_db[username]["total_spent"])
    flash(f"Đặt phòng {hotel_name} thành công! Giá: {price} VND", "success")
    return redirect(url_for("index"))

# ========================================



# === Hàm lấy dữ liệu ảnh khách sạn (đã có sẵn trong code bạn) ===
def get_hotel_gallery(hotel_name):
    folder_path = os.path.join("static", "images", "hotels", hotel_name)
    if not os.path.exists(folder_path):
        return []
    files = os.listdir(folder_path)
    return [
        f"/static/images/hotels/{hotel_name}/{f}"
        for f in files if f.lower() not in ["main.jng", "main.png"]
    ]
# Hàm đọc bài giới thiệu từ folder static/text/giới_thiệu
def read_intro(city_name):
    """
    city_name: tên chuẩn, ví dụ 'Hà Nội', 'TP Hồ Chí Minh', 'Đà Nẵng', 'Nha Trang'
    """
    # map city name -> tên file
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

    # Dữ liệu các địa danh
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
    # đọc bài giới thiệu
    info["intro"] = read_intro(info["name"])

    return render_template("destination.html", info=info)

# -------------------------
# ĐƯỜNG DẪN FILE (LINH HOẠT)
# -------------------------
# Nếu user để hotels.csv cùng thư mục với app.py thì dùng file đó,
# nếu không thì fallback sang thư mục data/.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FOLDER = os.path.join(BASE_DIR, 'data')
os.makedirs(DATA_FOLDER, exist_ok=True)

# ưu tiên file trong cùng thư mục với app.py (nếu tồn tại)
hotels_candidate = os.path.join(BASE_DIR, 'hotels.csv')
if os.path.exists(hotels_candidate):
    HOTELS_CSV = hotels_candidate
else:
    HOTELS_CSV = os.path.join(DATA_FOLDER, 'hotels.csv')

# bookings luôn dùng trong data (nếu bạn muốn khác có thể đổi)
BOOKINGS_CSV = os.path.join(DATA_FOLDER, 'bookings.csv')
REVIEWS_CSV = os.path.join(BASE_DIR, 'reviews.csv') if os.path.exists(os.path.join(BASE_DIR, 'reviews.csv')) else os.path.join(DATA_FOLDER, 'reviews.csv')

# === CẤU HÌNH EMAIL (giữ nguyên) ===
app.config.update(
    MAIL_SERVER='smtp.gmail.com',
    MAIL_PORT=587,
    MAIL_USE_TLS=True,
    MAIL_USE_SSL=False,
    MAIL_USERNAME='hotelpinder@gmail.com',   # Gmail thật
    MAIL_PASSWORD='znsj ynpd burr tdeo',     # Mật khẩu ứng dụng 16 ký tự (giữ như cũ)
    MAIL_DEFAULT_SENDER=('Hotel Pinder', 'hotelpinder@gmail.com')
)
mail = Mail(app)

# === FILE PATHS (Tạo bookings nếu chưa có) ===
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

# === ĐẢM BẢO FILE hotels/reviews (nếu không có thì báo) ===
if not os.path.exists(HOTELS_CSV):
    # nếu không có hotels.csv ở BASE_DIR hoặc data, báo lỗi để user bổ sung
    raise FileNotFoundError(f"❌ Không tìm thấy hotels.csv — đặt file ở: {HOTELS_CSV}")

if not os.path.exists(REVIEWS_CSV):
    pd.DataFrame(columns=["hotel_name", "user", "rating", "comment"]).to_csv(
        REVIEWS_CSV, index=False, encoding="utf-8-sig"
    )

# === HÀM ĐỌC CSV AN TOÀN (sửa để xử lý '5.0', dấu phẩy, v.v.) ===
def read_csv_safe(file_path):
    encodings = ["utf-8-sig", "utf-8", "cp1252"]
    for enc in encodings:
        try:
            # đọc tất cả cột dưới dạng str trước, sau đó convert numeric an toàn
            df = pd.read_csv(file_path, encoding=enc, dtype=str)
            df.columns = df.columns.str.strip()
            # các cột cần convert số
            numeric_cols = ['price', 'stars', 'rating', 'num_adults', 'num_children', 'nights', 'rooms_available']
            for col in numeric_cols:
                if col in df.columns:
                    # loại dấu phẩy, loại ".0" cuối, rồi convert numeric
                    df[col] = df[col].astype(str).str.replace(',', '').str.strip()
                    df[col] = df[col].str.replace(r'\.0$', '', regex=True)  # '5.0' -> '5'
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            return df
        except UnicodeDecodeError:
            continue
        except Exception as e:
            print(f"⚠️ Lỗi khi xử lý file {file_path}: {e}")
            raise
    raise UnicodeDecodeError(f"Không đọc được file {file_path} với UTF-8 hoặc cp1252!")

# === LOAD DỮ LIỆU BAN ĐẦU (vẫn load để có cấu trúc, nhưng routes đọc file tươi) ===
hotels = read_csv_safe(HOTELS_CSV)
reviews_df = read_csv_safe(REVIEWS_CSV)

if 'name' not in hotels.columns:
    if 'Name' in hotels.columns:
        hotels = hotels.rename(columns={'Name': 'name'})
    else:
        raise KeyError("❌ hotels.csv không có cột 'name'!")

if 'hotel_name' not in reviews_df.columns:
    raise KeyError("❌ reviews.csv không có cột 'hotel_name'.")


# === HÀM HỖ TRỢ MAPPING / ICON ===
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


# === TRANG CHỦ ===
@app.route('/')
def home():
    hotels_df = read_csv_safe(HOTELS_CSV)
    # đảm bảo cột rooms_available và status tồn tại và đúng kiểu
    if 'rooms_available' not in hotels_df.columns:
        hotels_df['rooms_available'] = 0
    hotels_df['rooms_available'] = hotels_df['rooms_available'].astype(int)
    if 'status' not in hotels_df.columns:
        hotels_df['status'] = hotels_df['rooms_available'].apply(lambda x: 'còn' if int(x) > 0 else 'hết')

    cities = sorted(hotels_df['city'].dropna().unique())
    return render_template('index.html', cities=cities)


# === TRANG GỢI Ý / FILTER NÂNG CAO ===
@app.route('/recommend', methods=['POST', 'GET'])
def recommend():
    filtered = read_csv_safe(HOTELS_CSV)

    # đảm bảo cột status và rooms_available tồn tại và đúng kiểu
    if 'rooms_available' not in filtered.columns:
        filtered['rooms_available'] = 0
    filtered['rooms_available'] = filtered['rooms_available'].astype(int)
    if 'status' not in filtered.columns:
        filtered['status'] = filtered['rooms_available'].apply(lambda x: 'còn' if x > 0 else 'hết')
    else:
        filtered['status'] = filtered['rooms_available'].apply(lambda x: 'còn' if x > 0 else 'hết')

    # --- Lấy dữ liệu từ form (POST) hoặc query string (GET) ---
    if request.method == 'POST':
        city = request.form.get('location', '').lower()
        budget = request.form.get('budget', '')
        stars = request.form.get('stars', '')
        amenities = request.form.getlist('amenities')  # danh sách checkbox
        size = request.form.get('size', '')
    else:
        city = request.args.get('location', '').lower()
        budget = request.args.get('budget', '')
        stars = request.args.get('stars', '')
        amenities = request.args.getlist('amenities')
        size = request.args.get('size', '')

    # --- Lọc theo thành phố ---
    if city:
        filtered = filtered[filtered['city'].str.lower() == city]

    # --- Lọc theo ngân sách ---
    if budget:
        try:
            budget = float(budget)
            filtered = filtered[filtered['price'] <= budget]
        except Exception:
            pass

    # --- Lọc theo số sao ---
    if stars:
        try:
            stars = int(stars)
            filtered = filtered[filtered['stars'] >= stars]
        except Exception:
            pass

    # --- Lọc theo tiện nghi ---
    for amen in amenities:
        if amen == 'pool':
            filtered = filtered[filtered['pool'] == True]
        elif amen == 'sea':
            filtered = filtered[(filtered.get('sea', False) == True) | (filtered.get('sea_view', False) == True)]
        elif amen == 'breakfast':
            filtered = filtered[filtered['buffet'] == True]
        elif amen == 'bar':
            filtered = filtered[filtered['bar'] == True]

    # --- Lọc theo loại phòng (diện tích) ---
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

    # --- Chuẩn bị kết quả ---
    results = [map_hotel_row(r) for r in filtered.to_dict(orient='records')]

    return render_template('result.html', hotels=results)


# === TRANG CHI TIẾT ===
@app.route('/hotel/<name>')
def hotel_detail(name):
    hotels_df = read_csv_safe(HOTELS_CSV)

    if 'rooms_available' not in hotels_df.columns:
        hotels_df['rooms_available'] = 0
    hotels_df['rooms_available'] = hotels_df['rooms_available'].astype(int)
    if 'status' not in hotels_df.columns:
        hotels_df['status'] = hotels_df['rooms_available'].apply(lambda x: 'còn' if int(x) > 0 else 'hết')
    else:
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

    # === THÊM GALLERY VÀO KHÁCH SẠN ===
    hotel['gallery'] = get_hotel_gallery(hotel['name'])
    # === THÊM EVENT IMAGE ===
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

# === GỬI ĐÁNH GIÁ ===
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

# === TRANG ĐẶT PHÒNG ===
@app.route('/booking/<name>/<room_type>', methods=['GET', 'POST'])
@app.route('/booking/<name>/<room_type>', methods=['GET', 'POST'])
def booking(name, room_type):
    hotels_df = read_csv_safe(HOTELS_CSV)
    hotels_df['rooms_available'] = hotels_df.get('rooms_available', 0).astype(int)
    hotels_df['status'] = hotels_df['rooms_available'].apply(lambda x: 'còn' if int(x) > 0 else 'hết')

    hotel_data = hotels_df[hotels_df['name'] == name]
    if hotel_data.empty:
        return "<h3>Không tìm thấy khách sạn!</h3>", 404

    hotel = map_hotel_row(hotel_data.iloc[0].to_dict())
    hotel['status'] = 'còn' if int(hotel_data.iloc[0]['rooms_available']) > 0 else 'hết'
    is_available = hotel['status'].lower() == 'còn'
    flash(f"Trạng thái phòng hiện tại: {hotel['status']}", "info")

    # Lấy rank & giá giảm
    user_rank = session.get('user', {}).get('rank', 'Đồng')
    base_price = float(hotel.get('price', 0))
    discounted_price = get_discounted_price(user_rank, base_price)

    if request.method == 'POST':
        # Lấy thông tin người đặt
        username = session.get('user', {}).get('username', 'Khách vãng lai')
        email = request.form.get('email', '').strip()  # email từ form, bắt buộc điền nếu chưa đăng nhập
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

        # Lưu booking vào CSV
        try:
            df = pd.read_csv(BOOKINGS_CSV, encoding="utf-8-sig")
        except FileNotFoundError:
            df = pd.DataFrame(columns=info.keys())
        df = pd.concat([df, pd.DataFrame([info])], ignore_index=True)
        df.to_csv(BOOKINGS_CSV, index=False, encoding="utf-8-sig")

        # Cập nhật user session & total_spent nếu đăng nhập
        if "user" in session:
            if username in users_db:
                users_db[username]['total_spent'] += info['price']
                save_users(users_db)
                session['user']['rank'] = get_user_rank(users_db[username]['total_spent'])

        # Gửi email cho khách nếu có
        if email:
            try:
                msg_user = Message(
                    subject="Xác nhận đặt phòng - Hotel Pinder",
                    recipients=[email]
                )
                msg_user.html = render_template("msg_user.html", info=info)
                mail.send(msg_user)
            except Exception as e:
                print(f"Lỗi gửi email cho khách: {e}")

        # Gửi email cho admin
        try:
            msg_admin = Message(
                subject=f"Đơn đặt phòng mới tại {info['hotel_name']}",
                recipients=["hotelpinder@gmail.com"]
            )
            msg_admin.html = f"""
                <h3>Đơn đặt phòng mới</h3>
                <p>Khách sạn: {info['hotel_name']}</p>
                <p>Người đặt: {info['user_name']}</p>
                <p>Email: {info['email']}</p>
                <p>SĐT: {info['phone']}</p>
                <p>Phòng: {info['room_type']}</p>
                <p>Ngày nhận: {info['checkin_date']}</p>
                <p>Số đêm: {info['nights']}</p>
                <p>Người lớn: {info['num_adults']} | Trẻ em: {info['num_children']}</p>
                <p>Ghi chú: {info['special_requests']}</p>
                <p>Giá: {info['price']}</p>
                <p>Mã đặt phòng: {info['booking_code']}</p>
            """
            mail.send(msg_admin)
        except Exception as e:
            print(f"Lỗi gửi email admin: {e}")

        flash("Đặt phòng thành công!", "success")
        return render_template('success.html', info=info)

    # GET request, hiển thị form booking
    return render_template('booking.html', hotel=hotel, room_type=room_type, 
                           is_available=is_available, discounted_price=discounted_price)

# === LỊCH SỬ ĐẶT PHÒNG ===
@app.route("/history")
def booking_history():
    # Kiểm tra user đăng nhập
    user = session.get("user")  # Lấy từ session
    if not user:
        flash("Bạn cần đăng nhập để xem lịch sử.", "danger")
        return redirect(url_for("login"))

    is_admin = user.get("rank", "").lower() == "admin"
    email = request.args.get("email") if is_admin else user["email"]

    # Lọc bookings theo email
    try:
        df = pd.read_csv(BOOKINGS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        df = pd.DataFrame()
    
    bookings = df[df['email'] == email].to_dict(orient="records") if not df.empty else []

    # Truyền user vào template
    return render_template("history.html", bookings=bookings, email=email, is_admin=is_admin, user=user)


# === TRANG GIỚI THIỆU ===
@app.route('/about')
def about_page():
    return render_template('about.html')

# === ĐĂNG NHẬP QUẢN TRỊ ===
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


# === ĐĂNG XUẤT ===
@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    flash("Đã đăng xuất!", "info")
    return redirect(url_for('admin_login'))


# === TRANG DASHBOARD QUẢN TRỊ ===
@app.route('/admin')
def admin_dashboard():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))

    # Đọc dữ liệu
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

    # Đọc file khách sạn
    df = pd.read_csv(HOTELS_CSV, encoding='utf-8-sig')

    # --- Đảm bảo các cột cần thiết có tồn tại ---
    if 'rooms_available' not in df.columns:
        df['rooms_available'] = 1
    if 'status' not in df.columns:
        df['status'] = 'còn'

    # --- Xử lý dữ liệu bị thiếu hoặc NaN ---
    # Chuyển kiểu an toàn (loại '5.0' -> '5', loại dấu phẩy)
    df['rooms_available'] = df['rooms_available'].astype(str).str.replace(',', '').str.strip()
    df['rooms_available'] = df['rooms_available'].str.replace(r'\.0$', '', regex=True)
    df['rooms_available'] = pd.to_numeric(df['rooms_available'], errors='coerce').fillna(0).astype(int)
    df['status'] = df['rooms_available'].apply(lambda x: 'còn' if x > 0 else 'hết')
    df.to_csv(HOTELS_CSV, index=False, encoding='utf-8-sig')


    # --- Thêm khách sạn mới ---
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

    # --- Cập nhật số phòng còn ---
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


# === Quản lý đặt phòng (Admin) ===
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


# === Xác nhận đặt phòng ===
@app.route('/admin/bookings/confirm/<booking_time>')
def admin_confirm_booking(booking_time):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))

    df = pd.read_csv(BOOKINGS_CSV, encoding='utf-8-sig')
    df.loc[df['booking_time'] == booking_time, 'status'] = 'Đã xác nhận'
    df.to_csv(BOOKINGS_CSV, index=False, encoding='utf-8-sig')
    flash("Đã xác nhận đặt phòng!", "success")
    return redirect(url_for('admin_bookings'))


# === Xóa đặt phòng ===
@app.route('/admin/bookings/delete/<booking_time>')
def admin_delete_booking(booking_time):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))

    df = pd.read_csv(BOOKINGS_CSV, encoding='utf-8-sig')
    df = df[df['booking_time'] != booking_time]
    df.to_csv(BOOKINGS_CSV, index=False, encoding='utf-8-sig')
    flash("Đã xóa đặt phòng!", "info")
    return redirect(url_for('admin_bookings'))


# === XÓA KHÁCH SẠN ===
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


# === CẬP NHẬT TRẠNG THÁI KHÁCH SẠN ===
@app.route('/admin/hotels/status/<name>/<status>')
def update_hotel_status(name, status):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    try:
        # --- Đọc CSV trước ---
        df = pd.read_csv(HOTELS_CSV, encoding='utf-8-sig')

        if name in df['name'].values:
            # ✅ Cập nhật trạng thái
            df.loc[df['name'] == name, 'status'] = status

            # ✅ Đồng bộ rooms_available
            if status.strip().lower() == 'còn':
                # Nếu admin set "còn" mà rooms_available = 0 thì tự đặt = 1
                df.loc[df['name'] == name, 'rooms_available'] = df.loc[df['name'] == name, 'rooms_available'].replace(0, 1)
            elif status.strip().lower() == 'hết':
                df.loc[df['name'] == name, 'rooms_available'] = 0

            # Đồng bộ lại status theo rooms_available để hiển thị đúng trên booking
            df['status'] = df['rooms_available'].apply(lambda x: 'còn' if x > 0 else 'hết')

            df.to_csv(HOTELS_CSV, index=False, encoding='utf-8-sig')
            flash(f"✅ Đã cập nhật {name} → {status}", "success")
        else:
            flash("⚠️ Không tìm thấy khách sạn này!", "warning")
    except Exception as e:
        flash(f"Lỗi khi cập nhật trạng thái: {e}", "danger")
    return redirect(url_for('admin_hotels'))


# ------------------------
# CẤU HÌNH GEMINI API
# ------------------------
try:
    GEMINI_API_KEY = os.environ.get("GOOGLE_API_KEY", "DÁN_GEMINI_API_KEY_CỦA_ANH_VÀO_ĐÂY")
    if not GEMINI_API_KEY or GEMINI_API_KEY == "DÁN_GEMINI_API_KEY_CỦA_ANH_VÀO_ĐÂY":
        print("CẢNH BÁO: GOOGLE_API_KEY chưa được set.")
    
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
except Exception as e:
    print(f"Lỗi khởi tạo Gemini: {e}")
    model = None # Đặt là None để kiểm tra sau
# ------------------------

@app.route('/ai_chat')
def ai_chat():
    return render_template('ai_chat_hotel.html')

#  TẠO "CẦU NỐI" (API ENDPOINT) CHO AI CHAT
@app.route('/api/chat', methods=['POST'])
def api_chat():
    if not model:
        return jsonify({"error": "Gemini AI chưa được cấu hình"}), 500
        
    try:
        user_query = request.json.get('query')
        include_hotels = request.json.get('include_hotels', True)
        conversation_history = request.json.get('history', [])  # Lấy lịch sử chat
        
        if not user_query:
            return jsonify({"error": "Missing query"}), 400

        # 1. Đọc và xử lý dữ liệu từ CSV
        hotels_data = []
        reviews_data = []
        events_data = []
        
        try:
            # Đọc hotels.csv
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
            
            # Đọc reviews.csv
            reviews_df = pd.read_csv("reviews.csv", encoding='utf-8-sig')
            for _, review in reviews_df.iterrows():
                review_info = {
                    'hotel_name': review.get('hotel_name', ''),
                    'user': review.get('user', 'Khách hàng'),
                    'rating': review.get('rating', 4.5),
                    'comment': review.get('comment', 'Trải nghiệm tuyệt vời!')
                }
                reviews_data.append(review_info)
            
            # Đọc events.csv - CẢI THIỆN: Đọc đầy đủ thông tin sự kiện
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
            # Fallback data với các khách sạn mẫu
            hotels_data = [
                {
                    'name': 'Sunrise Nha Trang',
                    'city': 'Nha Trang',
                    'district': 'Trần Phú',
                    'price': '2,500,000 VNĐ',
                    'rating': 4.8,
                    'amenities': 'Pool, Spa, Beach Front, Restaurant, Bar',
                    'description': 'Khách sạn 5 sao view biển tuyệt đẹp với hồ bơi vô cực'
                }
            ]
            
            # Fallback events data
            events_data = [
                {
                    'event_name': 'Lễ hội biển Nha Trang',
                    'city': 'Nha Trang',
                    'start_date': '2024-06-01',
                    'end_date': '2024-06-07',
                    'season': 'Hè',
                    'description': 'Lễ hội văn hóa biển với nhiều hoạt động hấp dẫn',
                    'best_time': 'Tháng 6-8',
                    'weather': 'Nắng đẹp, nhiệt độ 28-32°C'
                }
            ]

        # 2. Phân tích câu hỏi THÔNG MINH HƠN
        query_analysis = analyze_user_query(user_query, conversation_history)
        need_hotel_recommendation = query_analysis['need_hotel_recommendation']
        should_show_cards = query_analysis['should_show_cards']
        is_greeting = query_analysis['is_greeting']
        
        print(f"🔍 Query Analysis: {query_analysis}")

        # 3. Xây dựng prompt THÔNG MINH với CONTEXT
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

        # 4. Gọi Gemini
        max_retries = 2
        for attempt in range(max_retries):
            try:
                full_prompt = system_prompt + f"\n\nCâu hỏi: {user_query}"
                
                response = model.generate_content(
                    full_prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.3,  # Giảm temperature để ít sáng tạo hơn
                        max_output_tokens=1500
                    )
                )
                ai_response = response.text
                
                # Clean up response
                cleaned_response = clean_ai_response(ai_response, is_greeting, conversation_history)
                
                # Chuẩn bị dữ liệu trả về
                response_data = {"response": cleaned_response}
                
                # Chỉ trả về hotel data khi THỰC SỰ cần thiết
                if should_show_cards and include_hotels and need_hotel_recommendation:
                    recommended_hotels = get_recommended_hotels_from_ai_response(
                        hotels_data, reviews_data, user_query, cleaned_response, query_analysis
                    )
                    response_data["hotels"] = recommended_hotels[:3]
                    print(f"🏨 Showing {len(recommended_hotels[:3])} hotel cards")
                
                return jsonify(response_data)
                
            except Exception as e:
                if "quota" in str(e).lower() or "429" in str(e):
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        print(f"Quota exceeded, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    else:
                        return jsonify({"error": "Hệ thống đang quá tải. Vui lòng thử lại sau 1 phút."}), 429
                else:
                    raise e

        return jsonify({"error": "Lỗi kết nối. Vui lòng thử lại."}), 500

    except Exception as e:
        print(f"Lỗi API chat: {e}")
        return jsonify({"response": "Hiện tại hệ thống đang gặp sự cố kỹ thuật. Tôi vẫn muốn lắng nghe và hỗ trợ bạn. Hãy thử lại sau ít phút nhé!"})

# ========== CÁC HÀM HỖ TRỢ MỚI ==========

def analyze_user_query(user_query, conversation_history):
    """Phân tích câu hỏi người dùng THÔNG MINH HƠN"""
    query_lower = user_query.lower()
    
    # Chuẩn hóa từ viết tắt
    normalized_query = normalize_vietnamese_slang(query_lower)
    
    # Kiểm tra chào hỏi (chỉ chào khi bắt đầu)
    is_greeting = any(word in normalized_query for word in [
        'chào', 'hello', 'hi', 'xin chào', 'hey'
    ]) and len(conversation_history) == 0
    
    # Kiểm tra câu hỏi về khách sạn cụ thể (không hiển thị card)
    is_specific_hotel_inquiry = any(pattern in normalized_query for pattern in [
        'bạn biết khách sạn', 'bạn biết ks', 'bạn có biết khách sạn', 
        'bạn có biết ks', 'khách sạn này', 'ks này'
    ])
    
    # Kiểm tra cần đề xuất khách sạn
    need_hotel_recommendation = any(keyword in normalized_query for keyword in [
        'tìm khách sạn', 'đề xuất khách sạn', 'khách sạn nào', 'ở đâu',
        'tìm chỗ ở', 'booking', 'đặt phòng', 'recommend', 'suggest', 'hotel',
        'nghỉ ở đâu', 'chỗ ở', 'khách sạn', 'resort', 'nhà nghỉ', 'tư vấn khách sạn',
        'nên ở đâu', 'ở khách sạn nào'
    ]) and not is_specific_hotel_inquiry
    
    # Quyết định hiển thị card
    should_show_cards = need_hotel_recommendation and not is_specific_hotel_inquiry
    
    return {
        'is_greeting': is_greeting,
        'need_hotel_recommendation': need_hotel_recommendation,
        'should_show_cards': should_show_cards,
        'normalized_query': normalized_query,
        'is_specific_hotel_inquiry': is_specific_hotel_inquiry
    }

def normalize_vietnamese_slang(text):
    """Chuẩn hóa từ viết tắt tiếng Việt"""
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
    """Xây dựng thông tin sự kiện theo thành phố"""
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
        if event.get('weather'):
            event_info += f" - Thời tiết: {event.get('weather')}"
        if event.get('description'):
            event_info += f" - {event.get('description')}"
            
        city_events[city].append(event_info)
    
    result = []
    for city, events in city_events.items():
        result.append(f"{city}:")
        result.extend(events)
    
    return "\n".join(result) if result else "Hiện chưa có thông tin sự kiện."

def build_conversation_context(conversation_history):
    """Xây dựng context từ lịch sử hội thoại"""
    if not conversation_history or len(conversation_history) == 0:
        return "Đây là tin nhắn đầu tiên, có thể chào hỏi ngắn gọn."
    
    # Lấy 4 tin nhắn gần nhất để làm context
    recent_history = conversation_history[-4:] if len(conversation_history) > 4 else conversation_history
    
    context_lines = ["Lịch sử trò chuyện gần đây:"]
    for msg in recent_history:
        role = "User" if msg.get('role') == 'user' else "Assistant"
        content = msg.get('content', '')[:100]  # Giới hạn độ dài
        context_lines.append(f"{role}: {content}")
    
    context_lines.append("\nHãy tiếp tục cuộc trò chuyện một cách tự nhiên, KHÔNG chào lại.")
    return "\n".join(context_lines)

def clean_ai_response(ai_response, is_greeting, conversation_history):
    """Làm sạch response từ AI"""
    # Loại bỏ markdown
    cleaned = ai_response.replace('**', '').replace('*', '').strip()
    
    # Nếu không phải là lời chào đầu tiên, loại bỏ các câu chào không cần thiết
    if not is_greeting and len(conversation_history) > 0:
        greeting_patterns = [
            'xin chào', 'chào bạn', 'chào mừng', 'hello', 'hi ',
            'rất vui được gặp bạn', 'chào anh', 'chào chị'
        ]
        for pattern in greeting_patterns:
            if cleaned.lower().startswith(pattern):
                # Tìm vị trí kết thúc lời chào
                sentences = cleaned.split('.')
                if len(sentences) > 1:
                    # Giữ lại các câu sau lời chào
                    cleaned = '.'.join(sentences[1:]).strip()
                    if cleaned.startswith(','):
                        cleaned = cleaned[1:].strip()
                break
    
    return cleaned

def get_recommended_hotels_from_ai_response(hotels_data, reviews_data, user_query, ai_response, query_analysis):
    """Lấy khách sạn được đề xuất với độ chính xác cao - FIX ĐỒNG BỘ HOÀN TOÀN"""
    
    print(f"🔍 AI Response: {ai_response}")
    print(f"🏨 Available hotels: {[h['name'] + ' in ' + h.get('city', 'Unknown') for h in hotels_data]}")
    
    # Nếu là câu hỏi về khách sạn cụ thể, không trả về card
    if query_analysis.get('is_specific_hotel_inquiry', False):
        print("🚫 Specific hotel inquiry - no cards")
        return []
    
    # 1. PHÁT HIỆN THÀNH PHỐ TỪ QUERY VÀ AI RESPONSE
    target_city = extract_city_from_query(query_analysis.get('normalized_query', user_query.lower()))
    
    # Nếu không tìm thấy từ query, thử tìm từ AI response
    if not target_city:
        target_city = extract_city_from_query(ai_response.lower())
        print(f"🔍 Extracted city from AI response: {target_city}")
    
    # 2. TÌM KHÁCH SẠN ĐƯỢC AI NHẮC ĐẾN CỤ THỂ
    mentioned_hotels = []
    ai_response_lower = ai_response.lower()
    
    for hotel in hotels_data:
        hotel_name = hotel['name']
        hotel_name_lower = hotel_name.lower()
        hotel_city = hotel.get('city', '').lower().strip()
        
        # KIỂM TRA QUAN TRỌNG: Thành phố phải khớp
        if target_city and hotel_city != target_city.lower():
            continue  # Bỏ qua nếu không cùng thành phố
        
        # Tìm khách sạn được AI đề cập trong response
        name_found = False
        
        # Kiểm tra tên đầy đủ
        if hotel_name_lower in ai_response_lower:
            name_found = True
        else:
            # Kiểm tra từ khóa chính trong tên (loại bỏ từ chung)
            name_parts = [part for part in hotel_name_lower.split() 
                         if part not in ['khách', 'sạn', 'hotel', 'resort', '&', 'and', 'central'] and len(part) > 2]
            
            for part in name_parts:
                if part in ai_response_lower:
                    name_found = True
                    break
        
        if name_found:
            # Thêm review nếu có
            hotel_reviews = [r for r in reviews_data if r['hotel_name'] == hotel_name]
            if hotel_reviews:
                hotel['review'] = hotel_reviews[0]
            
            mentioned_hotels.append(hotel)
            print(f"✅ Found AI-mentioned hotel: {hotel_name} in {hotel_city}")
    
    if mentioned_hotels:
        print(f"🎯 Using {len(mentioned_hotels)} AI-mentioned hotels: {[h['name'] for h in mentioned_hotels]}")
        return mentioned_hotels[:3]
    
    # 3. NẾU KHÔNG TÌM THẤY KHÁCH SẠN ĐƯỢC NHẮC, DÙNG THUẬT TOÁN THÔNG MINH CÓ RÀNG BUỘC THÀNH PHỐ
    print("🔄 No AI-mentioned hotels found, using smart filtering with city constraint")
    
    # Đảm bảo target_city được xác định rõ ràng
    if not target_city:
        # Thử xác định thành phố từ context
        if 'nha trang' in user_query.lower() or 'nha trang' in ai_response.lower():
            target_city = 'Nha Trang'
        elif 'hồ chí minh' in user_query.lower() or 'hồ chí minh' in ai_response.lower() or 'sài gòn' in user_query.lower():
            target_city = 'Hồ Chí Minh'
        elif 'hà nội' in user_query.lower() or 'hà nội' in ai_response.lower():
            target_city = 'Hà Nội'
        elif 'đà nẵng' in user_query.lower() or 'đà nẵng' in ai_response.lower():
            target_city = 'Đà Nẵng'
    
    print(f"🔍 Final target city: {target_city}")
    
    filtered_hotels = smart_hotel_filtering_with_city_constraint(hotels_data, reviews_data, user_query, query_analysis, target_city)
    
    # 4. QUAN TRỌNG: Kiểm tra xem có nên hiển thị card không
    if filtered_hotels and should_show_hotel_cards(ai_response, filtered_hotels, target_city):
        return filtered_hotels[:3]
    
    print("🚫 Hotel cards don't match AI content - hiding cards")
    return []

def smart_hotel_filtering_with_city_constraint(hotels_data, reviews_data, user_query, query_analysis, target_city):
    """Lọc khách sạn thông minh với ràng buộc thành phố CHẶT CHẼ"""
    query_lower = query_analysis.get('normalized_query', user_query.lower())
    scored_hotels = []
    
    # Xác định tiêu chí từ query
    budget_range = extract_budget_from_query(query_lower)
    amenities_needed = extract_amenities_from_query(query_lower)
    hotel_type = extract_hotel_type_from_query(query_lower)
    
    print(f"🔍 Smart filtering with city constraint - City: {target_city}")
    
    for hotel in hotels_data:
        hotel_city = hotel.get('city', '').lower().strip()
        target_city_lower = target_city.lower() if target_city else ""
        
        # RÀNG BUỘC QUAN TRỌNG: Phải cùng thành phố
        if target_city and hotel_city != target_city_lower:
            print(f"❌ City mismatch - Skipping: {hotel['name']} ({hotel_city}) vs {target_city_lower}")
            continue
        
        score = 0
        
        # Điểm cơ bản cho khách sạn cùng thành phố
        score += 10
        
        # Điểm cho ngân sách
        if budget_range:
            hotel_price = extract_price_value(hotel.get('price', ''))
            if hotel_price:
                if budget_range[0] <= hotel_price <= budget_range[1]:
                    score += 8
                elif hotel_price <= budget_range[1] * 1.2:
                    score += 4
        
        # Điểm cho tiện ích
        if amenities_needed:
            hotel_amenities = hotel.get('amenities', '').lower()
            for amenity in amenities_needed:
                if amenity in hotel_amenities:
                    score += 3
        
        # Điểm cho loại khách sạn
        hotel_rating = hotel.get('rating', 0)
        if hotel_type == 'luxury' and hotel_rating >= 4.5:
            score += 5
        elif hotel_type == 'budget' and hotel_rating <= 4.0:
            score += 5
        elif hotel_type == 'midrange' and 4.0 < hotel_rating < 4.5:
            score += 5
        
        # Điểm cho đánh giá
        score += hotel_rating * 0.5
        
        # Thêm review nếu có
        hotel_reviews = [r for r in reviews_data if r['hotel_name'] == hotel['name']]
        if hotel_reviews:
            hotel['review'] = hotel_reviews[0]
            score += 2
        
        hotel['match_score'] = score
        scored_hotels.append(hotel)
        print(f"📊 Added to results: {hotel['name']} in {hotel_city} - Score: {score}")
    
    # Sắp xếp theo điểm
    scored_hotels.sort(key=lambda x: x.get('match_score', 0), reverse=True)
    
    if scored_hotels:
        result = scored_hotels[:3]
        print(f"🏨 Final filtered hotels: {[f'{h['name']} ({h.get('city', 'Unknown')}) - {h.get('match_score', 0):.1f}' for h in result]}")
        return result
    
    print("❌ No hotels matched the criteria")
    return []

def should_show_hotel_cards(ai_response, filtered_hotels, target_city):
    """Kiểm tra xem có nên hiển thị card khách sạn không - CẢI THIỆN"""
    ai_lower = ai_response.lower()
    
    # Kiểm tra nếu AI đang từ chối hoặc nói không có khách sạn
    denial_phrases = [
        'không tìm thấy', 'không có', 'chưa có', 'hiện không',
        'không thể', 'chưa thể', 'xin lỗi', 'rất tiếc',
        'không đề xuất', 'không recommend', 'không phù hợp'
    ]
    
    if any(phrase in ai_lower for phrase in denial_phrases):
        return False
    
    # Kiểm tra nếu AI đang đề cập đến khách sạn hoặc thành phố mục tiêu
    hotel_mention_phrases = [
        'khách sạn', 'resort', 'hotel', 'đề xuất', 'gợi ý',
        'sau đây', 'các lựa chọn', 'bạn có thể', 'nên chọn',
        'phù hợp', 'tốt nhất'
    ]
    
    # Kiểm tra đề cập đến thành phố mục tiêu
    city_mentioned = False
    if target_city:
        city_variations = {
            'nha trang': ['nha trang', 'nhatrang'],
            'hồ chí minh': ['hồ chí minh', 'sài gòn', 'thành phố hồ chí minh'],
            'hà nội': ['hà nội', 'hanoi'],
            'đà nẵng': ['đà nẵng', 'danang']
        }
        
        for city_key, variations in city_variations.items():
            if city_key in target_city.lower():
                city_mentioned = any(var in ai_lower for var in variations)
                break
    
    has_hotel_mentions = any(phrase in ai_lower for phrase in hotel_mention_phrases)
    
    print(f"🔍 Should show cards - Hotel mentions: {has_hotel_mentions}, City mentioned: {city_mentioned}")
    
    return has_hotel_mentions or city_mentioned

def normalize_city_name(city_name):
    """Chuẩn hóa tên thành phố để so sánh"""
    if not city_name:
        return ""
    
    city_mapping = {
        'hà nội': 'Hanoi', 'hanoi': 'Hanoi',
        'đà nẵng': 'Da Nang', 'danang': 'Da Nang', 
        'nha trang': 'Nha Trang', 'nhatrang': 'Nha Trang',
        'hồ chí minh': 'Ho Chi Minh', 'ho chi minh': 'Ho Chi Minh',
        'sài gòn': 'Ho Chi Minh'
    }
    
    city_lower = city_name.lower().strip()
    return city_mapping.get(city_lower, city_name)

def smart_hotel_filtering_with_city_constraint(hotels_data, reviews_data, user_query, query_analysis, target_city):
    """Lọc khách sạn thông minh với ràng buộc thành phố - FIXED VERSION"""
    query_lower = query_analysis.get('normalized_query', user_query.lower())
    scored_hotels = []
    
    # Xác định tiêu chí từ query
    budget_range = extract_budget_from_query(query_lower)
    amenities_needed = extract_amenities_from_query(query_lower)
    hotel_type = extract_hotel_type_from_query(query_lower)
    
    print(f"🔍 Smart filtering with city constraint - City: {target_city}")
    print(f"🔍 Available hotels in target city: {[h['name'] for h in hotels_data if h.get('city', '').lower() == target_city.lower()]}")
    
    for hotel in hotels_data:
        hotel_city = hotel.get('city', '').strip()
        
        # Sử dụng hàm chuẩn hóa để so sánh
        hotel_city_normalized = normalize_city_name(hotel_city)
        target_city_normalized = normalize_city_name(target_city) if target_city else ""
        
        # RÀNG BUỘC QUAN TRỌNG: So sánh đã được chuẩn hóa
        if target_city and hotel_city_normalized != target_city_normalized:
            print(f"❌ City mismatch - Skipping: {hotel['name']} ({hotel_city}) vs {target_city}")
            continue
        
        score = 0
        
        # Điểm cơ bản cho khách sạn cùng thành phố
        score += 10
        print(f"✅ City match: {hotel['name']} in {hotel_city}")
        
        # Điểm cho ngân sách
        if budget_range:
            hotel_price = extract_price_value(hotel.get('price', ''))
            if hotel_price:
                if budget_range[0] <= hotel_price <= budget_range[1]:
                    score += 8
                elif hotel_price <= budget_range[1] * 1.2:
                    score += 4
        
        # Điểm cho tiện ích
        if amenities_needed:
            hotel_amenities = hotel.get('amenities', '').lower()
            for amenity in amenities_needed:
                if amenity in hotel_amenities:
                    score += 3
        
        # Điểm cho loại khách sạn (5 sao)
        hotel_rating = hotel.get('rating', 0)
        if hotel_type == 'luxury' and hotel_rating >= 4.5:
            score += 10  # Tăng điểm mạnh cho khách sạn cao cấp
        elif hotel_type == 'budget' and hotel_rating <= 4.0:
            score += 5
        elif hotel_type == 'midrange' and 4.0 < hotel_rating < 4.5:
            score += 5
        
        # Điểm cho đánh giá
        score += hotel_rating * 0.5
        
        # Thêm review nếu có
        hotel_reviews = [r for r in reviews_data if r['hotel_name'] == hotel['name']]
        if hotel_reviews:
            hotel['review'] = hotel_reviews[0]
            score += 2
        
        hotel['match_score'] = score
        scored_hotels.append(hotel)
        print(f"📊 Added to results: {hotel['name']} in {hotel_city} - Score: {score}")
    
    # Sắp xếp theo điểm
    scored_hotels.sort(key=lambda x: x.get('match_score', 0), reverse=True)
    
    if scored_hotels:
        result = scored_hotels[:3]
        print(f"🏨 Final filtered hotels: {[f'{h['name']} ({h.get('city', 'Unknown')}) - {h.get('match_score', 0):.1f}' for h in result]}")
        return result
    
    print("❌ No hotels matched the criteria")
    return []

# Giữ nguyên các hàm extract_* từ bản trước
def extract_city_from_query(query):
    """Trích xuất thành phố từ query - FIXED VERSION"""
    city_mapping = {
        'hà nội': 'Hanoi', 'hanoi': 'Hanoi', 'ha noi': 'Hanoi',
        'đà nẵng': 'Da Nang', 'danang': 'Da Nang', 'da nang': 'Da Nang',
        'nha trang': 'Nha Trang', 'nhatrang': 'Nha Trang',
        'hồ chí minh': 'Ho Chi Minh', 'sài gòn': 'Ho Chi Minh', 
        'ho chi minh': 'Ho Chi Minh', 'hcm': 'Ho Chi Minh',
        'tp.hcm': 'Ho Chi Minh', 'tphcm': 'Ho Chi Minh'
    }
    
    query_lower = query.lower()
    
    # Tìm thành phố với độ ưu tiên cao (từ dài trước)
    sorted_cities = sorted(city_mapping.keys(), key=len, reverse=True)
    
    for keyword in sorted_cities:
        if keyword in query_lower:
            return city_mapping[keyword]
    
    return None

def extract_budget_from_query(query):
    """Trích xuất khoảng ngân sách từ query"""
    if 'triệu' in query or 'million' in query:
        if 'dưới 1' in query or 'dưới 2' in query or '1-2' in query:
            return (500000, 2000000)
        elif '2-3' in query or '2 đến 3' in query:
            return (2000000, 3000000)
        elif '3-5' in query or '3 đến 5' in query:
            return (3000000, 5000000)
        elif 'trên 5' in query or 'trên 5' in query:
            return (5000000, 10000000)
    
    return (1000000, 5000000)

def extract_amenities_from_query(query):
    """Trích xuất tiện ích từ query"""
    amenities = []
    amenity_mapping = {
        'hồ bơi': 'pool', 'pool': 'pool', 'bơi': 'pool',
        'spa': 'spa', 'massage': 'spa',
        'gym': 'gym', 'fitness': 'gym', 'thể hình': 'gym',
        'nhà hàng': 'restaurant', 'restaurant': 'restaurant',
        'bar': 'bar', 'quầy bar': 'bar',
        'biển': 'beach', 'beach': 'beach', 'view biển': 'beach'
    }
    
    for keyword, amenity in amenity_mapping.items():
        if keyword in query:
            amenities.append(amenity)
    
    return list(set(amenities))

def extract_hotel_type_from_query(query):
    """Trích xuất loại khách sạn từ query"""
    if any(word in query for word in ['sang trọng', 'luxury', '5 sao', 'năm sao', 'cao cấp']):
        return 'luxury'
    elif any(word in query for word in ['bình dân', 'budget', 'giá rẻ', 'tiết kiệm', '2 sao', '3 sao']):
        return 'budget'
    elif any(word in query for word in ['trung bình', 'mid-range', '4 sao']):
        return 'midrange'
    return None

def extract_price_value(price_str):
    """Chuyển đổi chuỗi giá thành số"""
    if not price_str or price_str == 'Liên hệ':
        return None
    
    try:
        clean_price = re.sub(r'[^\d]', '', str(price_str))
        if clean_price:
            return int(clean_price)
    except:
        pass
    
    return None

def google_search(query):
    """Hàm search web đơn giản"""
    try:
        # Có thể dùng SerpAPI, Google Custom Search API, hoặc search đơn giản
        search_url = f"https://www.google.com/search?q={requests.utils.quote(query + ' site:việt nam')}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(search_url, headers=headers, timeout=10)
        # Đây là ví dụ đơn giản, thực tế cần dùng API chính thức
        
        return f"Đã tìm thấy thông tin về: {query}"
        
    except Exception as e:
        return f"Không thể tìm kiếm thông tin: {str(e)}"


# -------------------------
# ROUTES SỰ KIỆN VÒNG QUAY TỬ THẦN
# -------------------------

@app.route('/event/user-info')
def event_user_info():
    """Lấy thông tin chi tiết của user cho sự kiện"""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    username = session['user']['username']
    
    # Tính tổng chi tiêu trong thời gian sự kiện
    total_spent = calculate_event_spending(username)
    
    # Lấy thông tin rank và lượt quay
    spin_info = get_max_spins(username)
    used_spins = get_used_spins(username)
    
    # Lấy lịch sử đặt phòng trong thời gian sự kiện
    event_bookings = []
    if os.path.exists(BOOKINGS_CSV):
        with open(BOOKINGS_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['username'] == username and row['status'] == 'completed':
                    try:
                        booking_time = datetime.strptime(row['booking_time'], '%Y-%m-%d %H:%M:%S')
                        if (booking_time.year == datetime.now().year and 
                            EVENT_CONFIG['start_month'] <= booking_time.month <= EVENT_CONFIG['end_month']):
                            event_bookings.append({
                                'hotel': row['hotel_name'],
                                'amount': float(row['price']),
                                'date': row['booking_time']
                            })
                    except:
                        continue
    
    return jsonify({
        'username': username,
        'rank': spin_info['rank'],
        'total_spent': total_spent,
        'spend_spins': spin_info['spend_spins'],
        'rank_bonus': spin_info['rank_bonus'],
        'total_spins': spin_info['total_spins'],
        'used_spins': used_spins,
        'spins_remaining': max(0, spin_info['total_spins'] - used_spins),
        'event_bookings': event_bookings,
        'event_period': f"{EVENT_CONFIG['start_month']}/8 - {EVENT_CONFIG['end_month']}/12"
    })

@app.route('/event')
def event_page():
    """Trang thông tin sự kiện"""
    return render_template('event.html')

@app.route('/event/check-eligibility')
def check_eligibility():
    """Kiểm tra điều kiện tham gia sự kiện"""
    if 'user' not in session:
        return jsonify({'eligible': False, 'message': 'Vui lòng đăng nhập'})
    
    current_month = datetime.now().month
    
    # Kiểm tra thời gian sự kiện (chỉ từ tháng 8-12 hàng năm)
    if current_month < EVENT_CONFIG['start_month'] or current_month > EVENT_CONFIG['end_month']:
        return jsonify({
            'eligible': False, 
            'message': f'Sự kiện chỉ diễn ra từ tháng {EVENT_CONFIG["start_month"]} đến tháng {EVENT_CONFIG["end_month"]} hàng năm',
            'event_active': False
        })
    
    username = session['user']['username']
    
    # Lấy thông tin từ users_db (đã có giải thưởng)
    user_data = users_db.get(username, {})
    total_spent = user_data.get('total_spent', 0)  # ✅ Đã có giải thưởng
    
    # Lấy thông tin lượt quay
    spin_info = get_max_spins(username)
    used_spins = get_used_spins(username)
    spins_remaining = max(0, spin_info['total_spins'] - used_spins)
    
    print(f"📊 Check eligibility: {username}, total_spent={total_spent:,}, spins_remaining={spins_remaining}")
    
    return jsonify({
        'eligible': spins_remaining > 0,
        'spins_remaining': spins_remaining,
        'total_spins': spin_info['total_spins'],
        'free_spin': spin_info['free_spin'],
        'spend_spins': spin_info['spend_spins'],
        'rank_bonus': spin_info['rank_bonus'],
        'rank': spin_info['rank'],
        'total_spent': total_spent,  # ✅ Tổng chi tiêu (cả giải thưởng)
        'used_spins': used_spins,
        'username': username,
        'event_active': True
    })

def check_event_bookings(username):
    """Kiểm tra user có booking trong thời gian sự kiện không"""
    if not os.path.exists(BOOKINGS_CSV):
        return False
    
    current_year = datetime.now().year
    
    with open(BOOKINGS_CSV, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['username'] == username and row['status'].lower() == 'completed':
                try:
                    booking_time = datetime.strptime(row['booking_time'], '%Y-%m-%d %H:%M:%S')
                    if (booking_time.year == current_year and 
                        EVENT_CONFIG['start_month'] <= booking_time.month <= EVENT_CONFIG['end_month']):
                        return True
                except:
                    continue
    return False

@app.route('/event/spin-wheel', methods=['POST'])
def spin_wheel():
    """Xử lý vòng quay"""
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    username = session['user']['username']
    
    # Kiểm tra và trừ lượt quay
    if not use_spin(username):
        used_spins = get_used_spins(username)
        total_spins = get_max_spins(username)['total_spins']
        if used_spins >= total_spins:
            return jsonify({'error': 'Bạn đã sử dụng hết lượt quay'}), 400
        else:
            return jsonify({'error': 'Không thể sử dụng lượt quay'}), 400
    
    # Quay thưởng
    prize = get_random_prize()
    
    # Cập nhật giải thưởng cho user (cộng vào tổng chi tiêu)
    if prize['value'] > 0:
        update_user_prize(username, prize['value'], prize['name'])
    
    # Tính góc quay cho hiệu ứng
    prize_index = next(i for i, p in enumerate(EVENT_CONFIG['prizes']) if p['value'] == prize['value'])
    sector_angle = 360 / len(EVENT_CONFIG['prizes'])
    final_angle = 360 - (prize_index * sector_angle + random.uniform(sector_angle * 0.1, sector_angle * 0.9))
    
    # Kiểm tra lượt quay còn lại
    total_spent = calculate_event_spending(username)
    used_spins = get_used_spins(username)
    spin_info = get_max_spins(username)
    spins_remaining = max(0, spin_info['total_spins'] - used_spins)
    
    return jsonify({
        'prize_name': prize['name'],
        'prize_value': prize['value'],
        'final_angle': final_angle,
        'spins_remaining': spins_remaining,
        'total_spent': total_spent,
        'free_spin': spin_info['free_spin'],
        'spend_spins': spin_info['spend_spins'],
        'rank_bonus': spin_info['rank_bonus'],
        'total_spins': spin_info['total_spins'],
        'used_spins': used_spins
    })

init_event_files()

# === KHỞI CHẠY APP ===
if __name__ == '__main__':
    app.run(debug=True)


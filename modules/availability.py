import os
import tempfile
import pandas as pd

def _safe_read_csv(path):
    """Read CSV with common encodings and normalize rooms_available column."""
    encodings = ["utf-8-sig", "utf-8", "cp1252"]
    last_exc = None
    for enc in encodings:
        try:
            df = pd.read_csv(path, encoding=enc, dtype=str)
            break
        except FileNotFoundError:
            # propagate FileNotFound so caller can decide
            raise
        except Exception as e:
            last_exc = e
            continue
    else:
        raise last_exc or Exception("Unable to read CSV")

    # Ensure rooms_available exists and is integer
    if 'rooms_available' not in df.columns:
        df['rooms_available'] = 0
    # Clean numeric formatting (commas, trailing .0)
    df['rooms_available'] = df['rooms_available'].astype(str).str.replace(',', '').str.strip()
    df['rooms_available'] = df['rooms_available'].str.replace(r'\.0$', '', regex=True)
    df['rooms_available'] = pd.to_numeric(df['rooms_available'], errors='coerce').fillna(0).astype(int)

    # Ensure status exists
    if 'status' not in df.columns:
        df['status'] = df['rooms_available'].apply(lambda x: 'còn' if int(x) > 0 else 'hết')

    return df

def _atomic_write_csv(df, path):
    """Write CSV to a temp file and atomically replace the target path."""
    dirpath = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(suffix='.csv', dir=dirpath)
    os.close(fd)
    # Use utf-8-sig for compatibility with Excel
    df.to_csv(tmp_path, index=False, encoding='utf-8-sig')
    os.replace(tmp_path, path)

def _match_rows(df, hotel_name):
    """Attempt to find rows matching hotel_name using common column names and fuzzy fallback."""
    hotel_name = str(hotel_name).strip()
    candidates = pd.Series([False] * len(df))

    # Prefer column named 'name' or 'hotel_name'
    if 'name' in df.columns:
        candidates = candidates | (df['name'].astype(str).str.strip() == hotel_name)
    if 'hotel_name' in df.columns:
        candidates = candidates | (df['hotel_name'].astype(str).str.strip() == hotel_name)

    # Case-insensitive fallback
    if not candidates.any():
        if 'name' in df.columns:
            candidates = candidates | (df['name'].astype(str).str.strip().str.lower() == hotel_name.lower())
        if 'hotel_name' in df.columns:
            candidates = candidates | (df['hotel_name'].astype(str).str.strip().str.lower() == hotel_name.lower())

    # Partial match fallback (contains)
    if not candidates.any():
        if 'name' in df.columns:
            candidates = candidates | (df['name'].astype(str).str.strip().str.lower().str.contains(hotel_name.lower(), na=False))
        if 'hotel_name' in df.columns:
            candidates = candidates | (df['hotel_name'].astype(str).str.strip().str.lower().str.contains(hotel_name.lower(), na=False))

    return candidates

def decrement_room_availability(hotels_csv_path, hotel_name, decrement=1):
    """
    Decrease rooms_available for hotel_name by `decrement` (default 1).
    Writes back hotels_csv_path atomically. Returns new rooms_available for the first matched hotel.
    If no hotel found, raises ValueError.
    """
    if decrement <= 0:
        raise ValueError("decrement must be positive")

    if not os.path.exists(hotels_csv_path):
        raise FileNotFoundError(f"Hotels CSV not found: {hotels_csv_path}")

    df = _safe_read_csv(hotels_csv_path)

    matches = _match_rows(df, hotel_name)
    if not matches.any():
        raise ValueError(f"No matching hotel found for name '{hotel_name}' in {hotels_csv_path}")

    # Apply decrement to matched rows (apply to first match to avoid affecting multiple rows unintentionally)
    first_idx = df[matches].index[0]
    current = int(df.at[first_idx, 'rooms_available'])
    new_val = max(0, current - int(decrement))
    df.at[first_idx, 'rooms_available'] = new_val
    df.at[first_idx, 'status'] = 'còn' if new_val > 0 else 'hết'

    # Persist atomically
    _atomic_write_csv(df, hotels_csv_path)

    return new_val

def increment_room_availability(hotels_csv_path, hotel_name, increment=1):
    """
    Increase rooms_available for hotel_name by `increment` (default 1).
    Returns the updated rooms_available for the first matched hotel.
    """
    if increment <= 0:
        raise ValueError("increment must be positive")
    if not os.path.exists(hotels_csv_path):
        raise FileNotFoundError(f"Hotels CSV not found: {hotels_csv_path}")

    df = _safe_read_csv(hotels_csv_path)

    matches = _match_rows(df, hotel_name)
    if not matches.any():
        raise ValueError(f"No matching hotel found for name '{hotel_name}' in {hotels_csv_path}")

    first_idx = df[matches].index[0]
    current = int(df.at[first_idx, 'rooms_available'])
    new_val = current + int(increment)
    df.at[first_idx, 'rooms_available'] = new_val
    df.at[first_idx, 'status'] = 'còn' if new_val > 0 else 'hết'

    _atomic_write_csv(df, hotels_csv_path)

    return new_val

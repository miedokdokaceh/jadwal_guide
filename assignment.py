import pandas as pd
import re
from datetime import datetime
from urllib.parse import quote
import gspread
from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe
import streamlit as st


# =========================================================
# AUTH
# =========================================================

def get_gspread_client():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    try:
        creds = Credentials.from_service_account_info(
            st.secrets["gcp_service_account"], scopes=scope
        )
    except Exception:
        creds = Credentials.from_service_account_file(
            "service_account.json", scopes=scope
        )
    return gspread.authorize(creds)


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def extract_date(text):
    """'1 Senin Juni 2026, ...' -> datetime"""
    match = re.search(r"(\d{1,2})\s(\w+)\s(\w+)\s(\d{4})", str(text))
    if match:
        day_num    = match.group(1)
        indo_month = match.group(3)
        year       = match.group(4)
        month_map  = {
            "Januari": "January", "Februari": "February", "Maret": "March",
            "April": "April", "Mei": "May", "Juni": "June",
            "Juli": "July", "Agustus": "August", "September": "September",
            "Oktober": "October", "November": "November", "Desember": "December",
        }
        translated = month_map.get(indo_month, indo_month)
        return datetime.strptime(f"{day_num} {translated} {year}", "%d %B %Y")
    return None


def extract_tanggal_num(text):
    """'1 Senin Juni 2026, ...' -> 1"""
    match = re.match(r"(\d{1,2})\s", str(text).strip())
    return int(match.group(1)) if match else None


def extract_shift(text):
    """
    Baca jam dari '(HH:MM)' lalu tentukan shift:
      P (Pagi)  : 00:00 - 11:59
      S (Sore)  : 12:00 - 17:59
      M (Malam) : 18:00 - 23:59
    """
    match = re.search(r"\((\d{1,2}):(\d{2})\)", str(text))
    if match:
        hour = int(match.group(1))
        if hour < 12:
            return "P"
        elif hour < 18:
            return "S"
        else:
            return "M"
    return None


# Kode unavailability di sheet -> shift yang terpengaruh
UNAVAILABILITY_COVERS = {
    "P":  {"P", "TS", "PM", "PS"},
    "S":  {"S", "TS", "SM", "PS"},
    "M":  {"M", "TS", "PM", "SM"},
}


def is_unavailable(unavail_code, shift_code):
    if not unavail_code or pd.isna(unavail_code):
        return False
    code = str(unavail_code).strip().upper()
    return code in UNAVAILABILITY_COVERS.get(shift_code, set())


def build_unavailability_dict(csv_url):
    """
    Baca sheet unavailability, cari baris header 'Guide' secara dinamis,
    return dict: { nama_guide: { tanggal_int: kode } }
    """
    raw = pd.read_csv(csv_url, header=None)

    header_row = None
    for i, row in raw.iterrows():
        if row.astype(str).str.strip().str.lower().eq("guide").any():
            header_row = i
            break

    if header_row is None:
        raise ValueError(
            "Kolom 'Guide' tidak ditemukan di sheet CHECK UNAVAILABILITY MONTHLY."
        )

    df = pd.read_csv(csv_url, header=header_row)
    df = df.rename(columns={"Guide": "Name"})

    # Kolom tanggal = kolom yang namanya berupa angka bulat
    date_cols = []
    for col in df.columns:
        try:
            int(col)
            date_cols.append(col)
        except (ValueError, TypeError):
            pass

    result = {}
    for _, row in df.iterrows():
        name = str(row.get("Name", "")).strip()
        if not name or name.lower() == "nan":
            continue
        unavail_by_date = {}
        for col in date_cols:
            val = row[col]
            if pd.notna(val) and str(val).strip():
                unavail_by_date[int(col)] = str(val).strip().upper()
        result[name] = unavail_by_date

    return result


# =========================================================
# MAIN FUNCTION
# =========================================================

def run_assignment():
    SPREADSHEET_ID       = "1oYpIm7qRNS69oWxgWPVPx1eOywvOsanr2VLaH7_pnSY"
    GS_UNAVAILABILITY_ID = "1jS8KUIYfCHAHafgibzr74GwCEBQvaObHSgoCqRiyGCA"

    # ---- Load Dashboard ----
    csv_url_dashboard = (
        f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
        f"/gviz/tq?tqx=out:csv&sheet=DASHBOARD"
    )
    dashboard = pd.read_csv(csv_url_dashboard, header=1)
    dashboard = dashboard[dashboard["SUDAH DIKIRIM"].notna()].copy()
    dashboard = dashboard[
        dashboard["SUDAH DIKIRIM"].astype(str).str.strip() != ""
    ]
    dashboard["DATE"]        = dashboard["TANGGAL & RUTE"].apply(extract_date)
    dashboard["TANGGAL_NUM"] = dashboard["TANGGAL & RUTE"].apply(extract_tanggal_num)
    dashboard["SHIFT"]       = dashboard["TANGGAL & RUTE"].apply(extract_shift)
    dashboard = dashboard[dashboard["DATE"].notna()].copy()
    dashboard["WEEK"] = dashboard["DATE"].dt.isocalendar().week
    dashboard = dashboard.sort_values(by="DATE", ascending=True).reset_index(drop=True)

    # ---- Load Unavailability ----
    encoded_sheet = quote("CHECK UNAVAILABILITY MONTHLY")
    csv_url_unavailability = (
        f"https://docs.google.com/spreadsheets/d/{GS_UNAVAILABILITY_ID}"
        f"/gviz/tq?tqx=out:csv&sheet={encoded_sheet}"
    )
    unavail_dict = build_unavailability_dict(csv_url_unavailability)

    # ---- Guide Dictionary ----
    guide_dict = {
        name: {"assigned_count": 0}
        for name in unavail_dict
    }

    # ---- Assignment per Minggu ----
    all_results = []
    weeks = sorted(dashboard["WEEK"].dropna().unique())

    for current_week in weeks:
        week_df = dashboard[dashboard["WEEK"] == current_week].copy()
        week_df = week_df.sort_values(by="DATE", ascending=True).reset_index(drop=True)

        for guide in guide_dict:
            guide_dict[guide]["assigned_count"] = 0

        output = []

        for _, row in week_df.iterrows():
            jadwal     = row["TANGGAL & RUTE"]
            tgl_num    = row["TANGGAL_NUM"]
            shift_code = row["SHIFT"]

            feasible = []
            for guide, info in guide_dict.items():
                kode = unavail_dict.get(guide, {}).get(tgl_num, "")
                if not is_unavailable(kode, shift_code):
                    feasible.append({
                        "guide": guide,
                        "k": info["assigned_count"],
                    })

            if not feasible:
                output.append({
                    "WEEK":             str(current_week),
                    "JADWAL":           jadwal,
                    "SHIFT":            shift_code,
                    "GUIDE_DITUGASKAN": "TIDAK ADA GUIDE",
                    "k_i":              "",
                    "TOTAL_DITUGASKAN": "0",
                })
                continue

            # Round-robin: pilih guide dengan penugasan paling sedikit
            chosen = min(feasible, key=lambda x: x["k"])
            guide_dict[chosen["guide"]]["assigned_count"] += 1

            output.append({
                "WEEK":             str(current_week),
                "JADWAL":           jadwal,
                "SHIFT":            shift_code,
                "GUIDE_DITUGASKAN": chosen["guide"],
                "k_i":              chosen["k"],
                "TOTAL_DITUGASKAN": str(guide_dict[chosen["guide"]]["assigned_count"]),
            })

        all_results.append(pd.DataFrame(output))

    # ---- Gabung & Sort ----
    assignment_df = pd.concat(all_results, ignore_index=True)
    assignment_df = assignment_df.merge(
        dashboard[["TANGGAL & RUTE", "DATE"]],
        left_on="JADWAL",
        right_on="TANGGAL & RUTE",
        how="left",
    )
    assignment_df = assignment_df.sort_values(
        by="DATE", ascending=True
    ).reset_index(drop=True)
    assignment_df = assignment_df.drop(columns=["TANGGAL & RUTE"])

    return assignment_df


# =========================================================
# EXPORT KE GOOGLE SHEETS
# =========================================================

def export_to_sheets(assignment_df):
    gc = get_gspread_client()
    spreadsheet = gc.open_by_key("1oYpIm7qRNS69oWxgWPVPx1eOywvOsanr2VLaH7_pnSY")

    try:
        worksheet = spreadsheet.worksheet("Penugasan")
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title="Penugasan", rows=5000, cols=20)

    worksheet.clear()
    set_with_dataframe(
        worksheet=worksheet,
        dataframe=assignment_df,
        include_index=False,
        include_column_header=True,
        resize=True,
    )

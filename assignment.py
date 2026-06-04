import pandas as pd
import re
from datetime import datetime
from urllib.parse import quote
import gspread
from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe
import streamlit as st


# =========================================================
# AUTH — Service Account
# =========================================================

def get_gspread_client():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    try:
        creds = Credentials.from_service_account_info(
            st.secrets["gcp_service_account"],
            scopes=scope,
        )
    except Exception:
        creds = Credentials.from_service_account_file(
            "service_account.json",
            scopes=scope,
        )

    return gspread.authorize(creds)


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def extract_date(text):
    """Ekstrak tanggal dari format: '1 Senin Juni 2026, PAKUALAMAN (15:30)'"""
    match = re.search(r"(\d{1,2})\s(\w+)\s(\w+)\s(\d{4})", str(text))
    if match:
        day_num = match.group(1)
        indo_month = match.group(3)
        year = match.group(4)
        month_map = {
            "Januari": "January", "Februari": "February", "Maret": "March",
            "April": "April", "Mei": "May", "Juni": "June",
            "Juli": "July", "Agustus": "August", "September": "September",
            "Oktober": "October", "November": "November", "Desember": "December",
        }
        translated_month = month_map.get(indo_month, indo_month)
        date_str = f"{day_num} {translated_month} {year}"
        return datetime.strptime(date_str, "%d %B %Y")
    return None


def extract_tanggal_num(text):
    """Ekstrak angka tanggal (1–31) dari TANGGAL & RUTE."""
    match = re.match(r"(\d{1,2})\s", str(text).strip())
    if match:
        return int(match.group(1))
    return None


def extract_shift(text):
    """
    Tentukan shift berdasarkan jam di TANGGAL & RUTE.
    Format jam: (HH:MM)
    - Pagi  : 00:00 – 11:59
    - Sore  : 12:00 – 17:59
    - Malam : 18:00 – 23:59
    """
    match = re.search(r"\((\d{1,2}):(\d{2})\)", str(text))
    if match:
        hour = int(match.group(1))
        if hour < 12:
            return "P"   # Pagi
        elif hour < 18:
            return "S"   # Sore
        else:
            return "M"   # Malam
    return None


# Kode unavailability yang mencakup masing-masing shift
UNAVAILABILITY_COVERS = {
    "P":  {"P", "TS", "PM", "PS"},   # Pagi
    "S":  {"S", "TS", "SM", "PS"},   # Sore
    "M":  {"M", "TS", "PM", "SM"},   # Malam
}


def is_guide_unavailable(unavail_code, shift_code):
    """
    Cek apakah kode unavailability dari sheet mencakup shift tertentu.
    unavail_code : nilai sel di sheet, misal 'TS', 'P', 'SM', dll.
    shift_code   : 'P', 'S', atau 'M'
    """
    if not unavail_code or pd.isna(unavail_code):
        return False
    code = str(unavail_code).strip().upper()
    if not code:
        return False
    covers = UNAVAILABILITY_COVERS.get(shift_code, set())
    return code in covers


def build_unavailability_dict(unavailability_gs):
    """
    Bangun dict: { guide_name: { tanggal_int: unavail_code } }
    dari DataFrame unavailability sheet (header=16, row 17 = header kolom).
    Kolom: No. | Guide | 1 | 2 | 3 | ... | 31
    """
    df = unavailability_gs.copy()
    df = df.rename(columns={"Guide": "Name"})

    # Kolom tanggal: kolom yang namanya bisa dikonversi ke integer
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
    csv_url = (
        f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
        f"/gviz/tq?tqx=out:csv&sheet=DASHBOARD"
    )
    dashboard = pd.read_csv(csv_url, header=1)
    dashboard = dashboard[dashboard["SUDAH DIKIRIM"].notna()].copy()
    dashboard = dashboard[
        dashboard["SUDAH DIKIRIM"].astype(str).str.strip() != ""
    ]
    dashboard["DATE"]         = dashboard["TANGGAL & RUTE"].apply(extract_date)
    dashboard["TANGGAL_NUM"]  = dashboard["TANGGAL & RUTE"].apply(extract_tanggal_num)
    dashboard["SHIFT"]        = dashboard["TANGGAL & RUTE"].apply(extract_shift)
    dashboard = dashboard[dashboard["DATE"].notna()].copy()
    dashboard["WEEK"] = dashboard["DATE"].dt.isocalendar().week
    dashboard = dashboard.sort_values(by="DATE", ascending=True).reset_index(drop=True)

    # ---- Load Unavailability ----
    encoded_sheet = quote("CHECK UNAVAILABILITY MONTHLY")
    csv_url_unavailability = (
        f"https://docs.google.com/spreadsheets/d/{GS_UNAVAILABILITY_ID}"
        f"/gviz/tq?tqx=out:csv&sheet={encoded_sheet}"
    )
    unavailability_gs = pd.read_csv(csv_url_unavailability, header=16)
    unavail_dict = build_unavailability_dict(unavailability_gs)

    # ---- Guide Dictionary ----
    guide_dict = {
        name: {"assigned_count": 0}
        for name in unavail_dict
    }

    # ---- Assignment Process ----
    all_assignment_results = []
    weeks = sorted(dashboard["WEEK"].dropna().unique())

    for current_week in weeks:
        dashboard_week = dashboard[dashboard["WEEK"] == current_week].copy()
        dashboard_week = dashboard_week.sort_values(
            by="DATE", ascending=True
        ).reset_index(drop=True)

        # Reset hitungan per minggu
        for guide in guide_dict:
            guide_dict[guide]["assigned_count"] = 0

        assignment_output = []

        for _, row in dashboard_week.iterrows():
            jadwal      = row["TANGGAL & RUTE"]
            tgl_num     = row["TANGGAL_NUM"]   # int, misal 1
            shift_code  = row["SHIFT"]         # 'P', 'S', atau 'M'

            feasible_guides = []

            for guide, info in guide_dict.items():
                guide_unavail = unavail_dict.get(guide, {})
                unavail_code  = guide_unavail.get(tgl_num, "")  # '' = kosong = available

                if not is_guide_unavailable(unavail_code, shift_code):
                    feasible_guides.append({
                        "guide": guide,
                        "k": info["assigned_count"],
                    })

            if len(feasible_guides) == 0:
                assignment_output.append({
                    "WEEK":             str(current_week),
                    "JADWAL":           jadwal,
                    "GUIDE_DITUGASKAN": "TIDAK ADA GUIDE",
                    "SHIFT":            shift_code,
                    "k_i":              "",
                    "TOTAL_DITUGASKAN": "0",
                })
                continue

            # Pilih guide dengan jumlah penugasan paling sedikit (round-robin)
            chosen = min(feasible_guides, key=lambda x: x["k"])
            guide_dict[chosen["guide"]]["assigned_count"] += 1
            total_ditugaskan = str(guide_dict[chosen["guide"]]["assigned_count"])

            assignment_output.append({
                "WEEK":             str(current_week),
                "JADWAL":           jadwal,
                "GUIDE_DITUGASKAN": chosen["guide"],
                "SHIFT":            shift_code,
                "k_i":              chosen["k"],
                "TOTAL_DITUGASKAN": total_ditugaskan,
            })

        assignment_df_week = pd.DataFrame(assignment_output)
        all_assignment_results.append(assignment_df_week)

    # ---- Gabung & Sort ----
    assignment_df = pd.concat(all_assignment_results, ignore_index=True)
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
    SPREADSHEET_ID_EXPORT = "1oYpIm7qRNS69oWxgWPVPx1eOywvOsanr2VLaH7_pnSY"
    sheet_name_export     = "Penugasan"

    spreadsheet = gc.open_by_key(SPREADSHEET_ID_EXPORT)

    try:
        worksheet = spreadsheet.worksheet(sheet_name_export)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=sheet_name_export, rows=5000, cols=20
        )

    worksheet.clear()
    set_with_dataframe(
        worksheet=worksheet,
        dataframe=assignment_df,
        include_index=False,
        include_column_header=True,
        resize=True,
    )

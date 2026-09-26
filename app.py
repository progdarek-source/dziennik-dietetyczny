import streamlit as st
import sqlite3
import fitz  # PyMuPDF do wyciągania zdjęć i tekstu z PDF
import re
import base64
from datetime import date
import math

# Próba zaimportowania sterownika Turso
try:
    import libsql_experimental as libsql
    HAS_LIBSQL = True
except ImportError:
    HAS_LIBSQL = False

st.set_page_config(page_title="Dziennik dietetyczny", layout="wide")

DEFAULT_IMAGE = "https://images.unsplash.com/photo-1490645935967-10de6ba17061?w=400"

# --- POŁĄCZENIE Z BAZĄ DANYCH (TURSO LUB LOCAL SQLITE) ---
def get_db_connection():
    """Łączy się z chmurową bazą Turso (jeśli podano klucze w st.secrets) lub z lokalną bazą SQLite."""
    if HAS_LIBSQL and "TURSO_DATABASE_URL" in st.secrets and "TURSO_AUTH_TOKEN" in st.secrets:
        return libsql.connect(
            database=st.secrets["TURSO_DATABASE_URL"],
            auth_token=st.secrets["TURSO_AUTH_TOKEN"]
        )
    return sqlite3.connect("przepisy.db")

# --- INICJALIZACJA BAZY DANYCH ---
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS przepisy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tytul TEXT,
            skladniki TEXT,
            przygotowanie TEXT,
            kcal INTEGER DEFAULT 500,
            bialko REAL DEFAULT 20.0,
            wegle REAL DEFAULT 50.0,
            tluszcze REAL DEFAULT 15.0,
            image_url TEXT DEFAULT '',
            zrodlo TEXT DEFAULT 'pdf'
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS planer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dzien TEXT,
            przepis_id INTEGER,
            FOREIGN KEY(przepis_id) REFERENCES przepisy(id) ON DELETE CASCADE
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dziennik (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dzien_data DATE,
            przepis_id INTEGER,
            porcje REAL DEFAULT 1.0,
            FOREIGN KEY(przepis_id) REFERENCES przepisy(id) ON DELETE CASCADE
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- PRZELICZANIE GRAMATURY W TEKŚCIE SKŁADNIKÓW ---
def scale_ingredients_text(text, factor):
    """Automatycznie przelicza gramaturę w tekście składników (np. 150 g -> 300 g)."""
    if factor == 1.0 or not text:
        return text
    
    def repl(match):
        val_str = match.group(1).replace(',', '.')
        unit = match.group(2)
        try:
            val = float(val_str) * factor
            val_formatted = f"{val:.1f}".rstrip('0').rstrip('.')
            return f"{val_formatted} {unit}"
        except:
            return match.group(0)
            
    pattern = r'(\d+(?:[\.,]\d+)?)\s*(g|ml|szt|sztuka|sztuki|sztuk|plastry|plastra|plastru|łyżka|łyżki|łyżeczka|łyżeczki|szczypta|opakowania|opakowanie|szklanka|szklanki|kromka|kromki|ząbek|ząbki)\b'
    return re.sub(pattern, repl, text, flags=re.IGNORECASE)

# --- PARSER PDF ---
def extract_meals_and_images_from_pdf(pdf_file):
    pdf_bytes = pdf_file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    extracted_recipes = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")

        if not re.search(r'Posiłek\s+\d+', text, re.I) and not re.search(r'\d+\s*Kcal', text, re.I):
            continue

        image_base64 = ""
        images = page.get_images(full=True)
        best_img_bytes = None
        max_size = 0
        
        for img_info in images:
            xref = img_info[0]
            base_image = doc.extract_image(xref)
            img_bytes = base_image["image"]
            if len(img_bytes) > max_size and len(img_bytes) > 5000:
                max_size = len(img_bytes)
                best_img_bytes = img_bytes
                ext = base_image["ext"]

        if best_img_bytes:
            encoded = base64.b64encode(best_img_bytes).decode('utf-8')
            image_base64 = f"data:image/{ext};base64,{encoded}"

        kcal_m = re.search(r'(\d+)\s*Kcal', text, re.I)
        b_m = re.search(r'(\d+(?:[\.,]\d+)?)\s*g?\s*B\b', text, re.I)
        w_m = re.search(r'(\d+(?:[\.,]\d+)?)\s*g?\s*W\b', text, re.I)
        t_m = re.search(r'(\d+(?:[\.,]\d+)?)\s*g?\s*T\b', text, re.I)

        kcal = int(kcal_m.group(1)) if kcal_m else 0
        b = float(b_m.group(1).replace(',', '.')) if b_m else 0.0
        w = float(w_m.group(1).replace(',', '.')) if w_m else 0.0
        t = float(t_m.group(1).replace(',', '.')) if t_m else 0.0

        raw_lines = [l.strip() for l in text.split('\n') if l.strip()]
        cleaned_lines = []

        for l in raw_lines:
            if re.search(r'błonnik|wapń|magnez|Respo:|Plan diety|Dzień\s*\d+', l, re.I):
                continue
            if re.search(r'^Posiłek\s*\d+(\s*/\s*\d{1,2}:\d{2}-\d{1,2}:\d{2})?$', l, re.I):
                continue
            if re.match(r'^\d{1,2}:\d{2}-\d{1,2}:\d{2}$', l):
                continue
            if re.search(r'Sposób przygotowania', l, re.I):
                continue
            if re.search(r'^\d+[\.,]?\s*(Kcal|g\s*[BWT]|g|mg)?$', l, re.I) or l.lower() in ['kcal', 'b', 'w', 't']:
                continue
            cleaned_lines.append(l)

        title_lines, ingredients, raw_steps_lines = [], [], []

        def is_ingredient(line_str):
            pattern = r'–|-|\(\d+\s*g\)\b|\b\d+(?:[\.,]\d+)?\s*(g|ml|szt|sztuka|sztuki|sztuk|plastry|plastra|plastru|łyżka|łyżki|łyżeczka|łyżeczki|szczypta|opakowania|opakowanie|szklanka|szklanki|kromka|kromki|ząbek|ząbki)\b'
            return bool(re.search(pattern, line_str, re.I))

        mode = 'title'
        for l in cleaned_lines:
            if mode == 'title':
                if is_ingredient(l) or re.match(r'^\d+[\.\)]\s*', l):
                    mode = 'content'
                else:
                    title_lines.append(l)
                    continue

            if mode == 'content':
                if is_ingredient(l) and not re.match(r'^\d+[\.\)]\s*[A-ZĄĆĘŁŃÓŚŹŻ]', l):
                    ingredients.append(l)
                else:
                    raw_steps_lines.append(l)

        tytul = " ".join(title_lines).strip()
        tytul = re.sub(r'^\d+\s*/\s*\d{1,2}:\d{2}-\d{1,2}:\d{2}\s*', '', tytul)
        tytul = re.sub(r'\s+', ' ', tytul)
        if not tytul:
            tytul = f"Przepis ze strony {page_num + 1}"

        merged_steps = []
        current_step = ""

        for line in raw_steps_lines:
            clean_l = re.sub(r'^\d+[\.\)]\s*', '', line).strip()
            if not clean_l:
                continue

            if current_step and (line[0].isupper() or re.match(r'^\d+[\.\)]', line)):
                if current_step.endswith(('.', '!', '?', ':')) or re.match(r'^\d+[\.\)]', line):
                    merged_steps.append(current_step)
                    current_step = clean_l
                else:
                    current_step += " " + clean_l
            else:
                if current_step:
                    current_step += " " + clean_l
                else:
                    current_step = clean_l

        if current_step:
            merged_steps.append(current_step)

        skladniki_txt = "\n".join(ingredients)
        przygotowanie_txt = "\n".join([f"{idx}. {step}" for idx, step in enumerate(merged_steps, 1)])

        extracted_recipes.append({
            "tytul": tytul,
            "skladniki": skladniki_txt,
            "przygotowanie": przygotowanie_txt,
            "kcal": kcal,
            "bialko": b,
            "wegle": w,
            "tluszcze": t,
            "image_url": image_base64
        })

    return extracted_recipes

# --- NAWIGACJA ---
st.sidebar.title("🥗 Dziennik dietetyczny")
opcja = st.sidebar.radio(
    label="Nawigacja",
    options=["📖 Baza Posiłków", "➕ Dodaj Przepis", "📅 Planer & Zakupy", "📊 Dziennik Dietetyczny"]
)

# --- 1. BAZA POSIŁKÓW ---
if opcja == "📖 Baza Posiłków":
    st.title("📖 Baza Przepisów")

    c_search, c_source, c_slider = st.columns([2, 1.2, 2])
    with c_search:
        szukaj = st.text_input("🔍 Szukaj po nazwie lub składniku:")
    with c_source:
        filtr_zrodlo = st.selectbox("📂 Źródło:", ["Wszystkie", "📄 Z PDF", "✍️ Dodane ręcznie"])
    with c_slider:
        wybrana_kalorycznosc = st.slider("🔥 Maks. kaloryczność (dla 1 porcji)", min_value=100, max_value=3000, value=1500, step=50)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, tytul, skladniki, przygotowanie, kcal, bialko, wegle, tluszcze, image_url, zrodlo FROM przepisy WHERE kcal <= ?", (wybrana_kalorycznosc,))
    rows = cursor.fetchall()
    conn.close()

    if szukaj:
        szukaj_l = szukaj.lower()
        rows = [r for r in rows if szukaj_l in r[1].lower() or (r[2] and szukaj_l in r[2].lower())]
        
    if filtr_zrodlo == "📄 Z PDF":
        rows = [r for r in rows if r[9] in ('pdf', None, '')]
    elif filtr_zrodlo == "✍️ Dodane ręcznie":
        rows = [r for r in rows if r[9] == 'recznie']

    st.caption(f"Znaleziono przepisów: {len(rows)}")

    NA_STRONE = 5
    lacznie_stron = math.ceil(len(rows) / NA_STRONE) if rows else 1
    
    if lacznie_stron > 1:
        col_pag1, col_pag2 = st.columns([1, 4])
        with col_pag1:
            aktualna_strona = st.number_input("Strona:", min_value=1, max_value=lacznie_stron, value=1, step=1)
        with col_pag2:
            st.write(f"\nWyświetlanie strony **{aktualna_strona}** z **{lacznie_stron}**")
    else:
        aktualna_strona = 1

    start_idx = (aktualna_strona - 1) * NA_STRONE
    end_idx = start_idx + NA_STRONE
    wyswietlane_rows = rows[start_idx:end_idx]

    for r in wyswietlane_rows:
        p_id, tytul, skladniki, przygotowanie, kcal, b, w, t, img_url, zrodlo = r
        
        header_text = f"{tytul} — 🔥 {kcal} kcal | B: {b:.1f}g | W: {w:.1f}g | T: {t:.1f}g"
        
        with st.expander(header_text):
            c_porcja, c_img_btn = st.columns([2, 1])
            with c_porcja:
                porcja = st.number_input("⚖️ Liczba porcji (przelicznik makro i gramatury):", min_value=0.1, max_value=10.0, value=1.0, step=0.1, key=f"porcja_{p_id}")
            
            p_kcal = int(kcal * porcja)
            p_b = round(b * porcja, 1)
            p_w = round(w * porcja, 1)
            p_t = round(t * porcja, 1)

            m1, m2, m3, m4 = st.columns(4)
            m1.warning(f"🔥 {p_kcal} kcal")
            m2.info(f"🏋️ B: {p_b}g")
            m3.success(f"🌾 W: {p_w}g")
            m4.error(f"🥑 T: {p_t}g")

            display_img = img_url if img_url and len(img_url) > 10 else DEFAULT_IMAGE
            with c_img_btn:
                with st.popover("📷 Pokaż zdjęcie potrawy"):
                    st.image(display_img, use_container_width=True)

            col_skladniki, col_kroki = st.columns(2)
            with col_skladniki:
                st.markdown("**Składniki (przeliczone):**")
                skladniki_scaled = scale_ingredients_text(skladniki, porcja)
                st.text(skladniki_scaled if skladniki_scaled else "Brak wyszczególnionych składników")
                
            with col_kroki:
                st.markdown("**Sposób przygotowania:**")
                st.text(przygotowanie if przygotowanie else "Brak podanych kroków")

            col_btn1, col_btn2 = st.columns(2)
            with col_btn1:
                with st.expander("✏️ Edytuj Przepis"):
                    with st.form(key=f"edit_form_{p_id}"):
                        new_tytul = st.text_input("Nazwa przepisu", value=tytul)
                        new_kcal = st.number_input("Kalorie (kcal)", value=kcal)
                        new_b = st.number_input("Białko (g)", value=b)
                        new_w = st.number_input("Węglowodany (g)", value=w)
                        new_t = st.number_input("Tłuszcze (g)", value=t)
                        new_skladniki = st.text_area("Składniki", value=skladniki, height=150)
                        new_przygotowanie = st.text_area("Sposób przygotowania", value=przygotowanie, height=150)
                        
                        if st.form_submit_button("💾 Zapisz zmiany"):
                            conn = get_db_connection()
                            cursor = conn.cursor()
                            cursor.execute("""
                                UPDATE przepisy 
                                SET tytul=?, kcal=?, bialko=?, wegle=?, tluszcze=?, skladniki=?, przygotowanie=?
                                WHERE id=?
                            """, (new_tytul, new_kcal, new_b, new_w, new_t, new_skladniki, new_przygotowanie, p_id))
                            conn.commit()
                            conn.close()
                            st.success("Zapisano zmiany!")
                            st.rerun()

            with col_btn2:
                if st.button("🗑️ Usuń ten przepis", key=f"del_{p_id}"):
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM przepisy WHERE id = ?", (p_id,))
                    conn.commit()
                    conn.close()
                    st.success(f"Usunięto: {tytul}")
                    st.rerun()

# --- 2. DODAWANIE PRZEPISÓW ---
elif opcja == "➕ Dodaj Przepis":
    st.title("➕ Dodaj Przepis")
    
    tryb = st.radio("Wybierz sposób dodania:", ["📄 Wgraj z pliku PDF", "✍️ Dodaj ręcznie"])
    
    if tryb == "📄 Wgraj z pliku PDF":
        uploaded_file = st.file_uploader("Wgraj plik PDF z przepisami (np. Dieta Respo)", type=["pdf"])

        if uploaded_file is not None:
            if st.button("🚀 Przetwórz i dodaj posiłki z pliku PDF"):
                try:
                    recipes = extract_meals_and_images_from_pdf(uploaded_file)
                    
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    
                    for r in recipes:
                        cursor.execute(
                            '''INSERT INTO przepisy (tytul, skladniki, przygotowanie, kcal, bialko, wegle, tluszcze, image_url, zrodlo) 
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pdf')''',
                            (r["tytul"], r["skladniki"], r["przygotowanie"], r["kcal"], r["bialko"], r["wegle"], r["tluszcze"], r["image_url"])
                        )
                    
                    conn.commit()
                    conn.close()
                    st.success(f"Pomyślnie dodano {len(recipes)} posiłków wraz z ich oryginalnymi zdjęciami!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Błąd podczas przetwarzania pliku: {e}")

    elif tryb == "✍️ Dodaj ręcznie":
        with st.form("manual_add_form"):
            st.subheader("Formularz ręcznego dodawania przepisu")
            m_tytul = st.text_input("Nazwa potrawy / przepisu *")
            
            c1, c2, c3, c4 = st.columns(4)
            m_kcal = c1.number_input("Kalorie (kcal)", value=500)
            m_b = c2.number_input("Białko (g)", value=25.0)
            m_w = c3.number_input("Węglowodany (g)", value=50.0)
            m_t = c4.number_input("Tłuszcze (g)", value=15.0)
            
            m_img = st.text_input("Link URL do zdjęcia (opcjonalnie)")
            m_skladniki = st.text_area("Składniki (każdy od nowej linii)", height=150)
            m_przygotowanie = st.text_area("Sposób przygotowania", height=150)
            
            if st.form_submit_button("➕ Dodaj przepis do bazy"):
                if not m_tytul.strip():
                    st.error("Podaj nazwę przepisu!")
                else:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO przepisy (tytul, skladniki, przygotowanie, kcal, bialko, wegle, tluszcze, image_url, zrodlo)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'recznie')
                    """, (m_tytul, m_skladniki, m_przygotowanie, m_kcal, m_b, m_w, m_t, m_img))
                    conn.commit()
                    conn.close()
                    st.success(f"Dodano przepis: {m_tytul}")

    st.markdown("---")
    if st.button("🗑️ Wyczyść całą bazę przepisów (Reset Bazy)"):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM przepisy")
        cursor.execute("DELETE FROM planer")
        cursor.execute("DELETE FROM dziennik")
        conn.commit()
        conn.close()
        st.success("Baza została całkowicie wyczyszczona!")
        st.rerun()

# --- 3. PLANER & ZAKUPY ---
elif opcja == "📅 Planer & Zakupy":
    st.title("📅 Planer Tygodniowy & Lista Zakupów")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, tytul, kcal FROM przepisy")
    przepisy_list = cursor.fetchall()
    dict_przepisy = {f"{p[1]} ({p[2]} kcal)": p[0] for p in przepisy_list}
    
    dni = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]
    
    st.subheader("1. Zaplanuj Posiłki na Tydzień")
    
    for dzien in dni:
        cursor.execute("SELECT przepis_id FROM planer WHERE dzien = ?", (dzien,))
        saved = [r[0] for r in cursor.fetchall()]
        
        default_options = [k for k, v in dict_przepisy.items() if v in saved]
        selected = st.multiselect(f"📆 {dzien}:", options=list(dict_przepisy.keys()), default=default_options, key=f"plan_{dzien}")
        
        cursor.execute("DELETE FROM planer WHERE dzien = ?", (dzien,))
        for sel in selected:
            cursor.execute("INSERT INTO planer (dzien, przepis_id) VALUES (?, ?)", (dzien, dict_przepisy[sel]))
    
    conn.commit()

    st.markdown("---")
    st.subheader("2. Generuj Listę Zakupów")

    if "lista_zakupow" not in st.session_state:
        st.session_state["lista_zakupow"] = []

    if st.button("🛒 Wygeneruj Listę Zakupów na Zaplanowany Tydzień"):
        cursor.execute("""
            SELECT p.skladniki 
            FROM planer pl 
            JOIN przepisy p ON pl.przepis_id = p.id
        """)
        zaplanowane = cursor.fetchall()
        
        if not zaplanowane:
            st.warning("Nie wybrano żadnych posiłków w planerze!")
            st.session_state["lista_zakupow"] = []
        else:
            skladniki = []
            for t in zaplanowane:
                if t[0]:
                    for line in t[0].split('\n'):
                        if line.strip():
                            skladniki.append(line.strip())
            
            st.session_state["lista_zakupow"] = sorted(list(set(skladniki)))

    if st.session_state["lista_zakupow"]:
        st.success("Wygenerowano listę składników:")
        for idx, item in enumerate(st.session_state["lista_zakupow"]):
            st.checkbox(item, key=f"shop_{idx}_{item}")

    conn.close()

# --- 4. DZIENNIK DIETETYCZNY ---
elif opcja == "📊 Dziennik Dietetyczny":
    st.title("📊 Dziennik Dietetyczny")

    dzisiejsza_data = st.date_input("Wybierz dzień:", value=date.today())
    # Konwersja obiektu date na napis w formacie YYYY-MM-DD
    dzisiejsza_data_str = str(dzisiejsza_data)

    conn = sqlite3.connect("przepisy.db")
    cursor = conn.cursor()
    
    cel_kcal = st.number_input("Twój cel kaloryczny (kcal):", value=2000, step=50)
    
    cursor.execute("SELECT id, tytul, kcal, bialko, wegle, tluszcze FROM przepisy")
    wszystkie = cursor.fetchall()
    dict_all = {f"{p[1]} ({p[2]} kcal)": p for p in wszystkie}
    
    c_add1, c_add2 = st.columns([3, 1])
    with c_add1:
        zjedzone = st.selectbox("Zjadłeś posiłek? Dodaj go do dziennika:", options=["-- Wybierz posiłek --"] + list(dict_all.keys()))
    with c_add2:
        ile_porcji_dziennik = st.number_input("Liczba porcji:", min_value=0.1, max_value=10.0, value=1.0, step=0.1)

    if st.button("➕ Dodaj do dzisiejszego dziennika") and zjedzone != "-- Wybierz posiłek --":
        p_id = dict_all[zjedzone][0]
        try:
            cursor.execute("INSERT INTO dziennik (dzien_data, przepis_id, porcje) VALUES (?, ?, ?)", (dzisiejsza_data_str, p_id, ile_porcji_dziennik))
        except:
            cursor.execute("INSERT INTO dziennik (dzien_data, przepis_id, porcja) VALUES (?, ?, ?)", (dzisiejsza_data_str, p_id, ile_porcji_dziennik))
        conn.commit()
        st.success("Dodano posiłek!")
        st.rerun()

    try:
        cursor.execute("""
            SELECT p.id, p.tytul, p.kcal, p.bialko, p.wegle, p.tluszcze, d.id, d.porcje
            FROM dziennik d
            JOIN przepisy p ON d.przepis_id = p.id
            WHERE d.dzien_data = ?
        """, (dzisiejsza_data_str,))
        eaten_rows = cursor.fetchall()
    except:
        cursor.execute("""
            SELECT p.id, p.tytul, p.kcal, p.bialko, p.wegle, p.tluszcze, d.id, d.porcja
            FROM dziennik d
            JOIN przepisy p ON d.przepis_id = p.id
            WHERE d.dzien_data = ?
        """, (dzisiejsza_data_str,))
        eaten_rows = cursor.fetchall()

    st.markdown("---")
    st.subheader(f"Podsumowanie spożycia z dnia: {dzisiejsza_data_str}")

    sum_kcal = sum(r[2] * (r[7] if len(r) > 7 and r[7] else 1.0) for r in eaten_rows)
    sum_b = sum(r[3] * (r[7] if len(r) > 7 and r[7] else 1.0) for r in eaten_rows)
    sum_w = sum(r[4] * (r[7] if len(r) > 7 and r[7] else 1.0) for r in eaten_rows)
    sum_t = sum(r[5] * (r[7] if len(r) > 7 and r[7] else 1.0) for r in eaten_rows)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Spożyte Kalorie", f"{int(sum_kcal)} / {cel_kcal} kcal", delta=f"{int(sum_kcal - cel_kcal)} kcal")
    m2.metric("Suma Białka", f"{sum_b:.1f} g")
    m3.metric("Suma Węglowodanów", f"{sum_w:.1f} g")
    m4.metric("Suma Tłuszczu", f"{sum_t:.1f} g")

    st.progress(min(sum_kcal / cel_kcal, 1.0) if cel_kcal > 0 else 0)

    st.markdown("**Lista posiłków zjedzonych dzisiaj:**")
    for row in eaten_rows:
        e_id, e_title, e_kcal, e_b, e_w, e_t, log_id, e_porcja = row
        p_factor = e_porcja if e_porcja else 1.0
        c_t, c_del = st.columns([4, 1])
        c_t.write(f"• **{e_title}** ({p_factor} porcja/e) — {int(e_kcal * p_factor)} kcal (B: {e_b * p_factor:.1f}g | W: {e_w * p_factor:.1f}g | T: {e_t * p_factor:.1f}g)")
        if c_del.button("❌ Usuń", key=f"del_log_{log_id}"):
            cursor.execute("DELETE FROM dziennik WHERE id = ?", (log_id,))
            conn.commit()
            st.rerun()

    conn.close()
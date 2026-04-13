import streamlit as st
import fitz
from docx import Document as DocxReader
from pdf2image import convert_from_bytes
import pytesseract
from PIL import Image, ImageDraw, ImageFont
from anthropic import Anthropic
import io, os, re, textwrap, zipfile, random

# ─── Page config ────────────────────────────────────────────────────
st.set_page_config(
    page_title="Product Catalog Generator",
    page_icon="📋",
    layout="wide"
)

# ─── Constants ──────────────────────────────────────────────────────
W, H = 1200, 1680

FONT_BOLD_PATHS = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
]
FONT_REG_PATHS = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
]

SYSTEM_PROMPT = """
You are a product catalog writer for an FMCG company.
Audience: Field Sales Representatives.

Generate a product catalog entry with EXACTLY the structure below.
Do not add preamble, closing remarks, or extra sections.
Use simple, direct language. Reading level: Class 8-10.

════════════════════════════════════
PRODUCT NAME: [Full product name]
PRODUCT TAGLINE: [One punchy line, max 10 words]
════════════════════════════════════

SECTION 1: PRODUCT USP
────────────────────────────────────
USP HEADLINE: [One bold sentence — the single most compelling reason to buy]
USP BODY: [2-3 sentences on what makes this product different from generics]

SECTION 2: PRODUCT'S COMPETITIVE EDGE
────────────────────────────────────
List exactly 4 bullet points. Each describes a specific advantage over
competing products. Start each with a dash ( - ).
Where numbers/stats exist in the source, use them.

SECTION 3: PRODUCT RANGE
────────────────────────────────────
List each product in the range. One per line.
Format: **[Product Name]** — [one sentence description, pack size if known]

SECTION 4: USAGE INSTRUCTIONS
────────────────────────────────────
Number each step (1., 2., 3. etc.). Max 6 steps. Min 3 steps.
End with: IMPORTANT NOTES: [cautions or tips]

SECTION 5: SELLING TACTICS FOR SALES REPS
────────────────────────────────────
Exactly 5 tactics. Format each as:
TACTIC [number]: [TACTIC NAME IN CAPS]
HOW: [1-2 sentences — what to say or do with retailer/consumer]

Cover: demo, objection handling, shelf placement, consumer trial, cross-sell.
"""

# ─── Font helper ────────────────────────────────────────────────────
@st.cache_resource
def get_font_paths():
    bold = next((p for p in FONT_BOLD_PATHS if os.path.exists(p)), None)
    reg  = next((p for p in FONT_REG_PATHS  if os.path.exists(p)), None)
    return bold, reg

def fnt(size, bold=False):
    bold_path, reg_path = get_font_paths()
    path = bold_path if bold else reg_path
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()

# ─── Extraction helpers ─────────────────────────────────────────────
def extract_text_from_pdf(file_bytes):
    doc = fitz.open(stream=file_bytes, filetype='pdf')
    text = ''
    for i, page in enumerate(doc):
        t = page.get_text()
        if t.strip():
            text += f'[Page {i+1}]\n{t}\n'
    return text

def extract_text_ocr(file_bytes):
    text = ''
    images = convert_from_bytes(file_bytes, dpi=200)
    for i, img in enumerate(images):
        t = pytesseract.image_to_string(img, lang='eng')
        if t.strip():
            text += f'[Page {i+1}]\n{t}\n'
    return text

def extract_text_from_docx(file_bytes):
    doc = DocxReader(io.BytesIO(file_bytes))
    text = ''
    for p in doc.paragraphs:
        if p.text.strip():
            text += p.text + '\n'
    for table in doc.tables:
        for row in table.rows:
            rt = ' | '.join(
                c.text.strip() for c in row.cells if c.text.strip()
            )
            if rt:
                text += rt + '\n'
    return text

def extract_images_from_pdf(file_bytes, min_size=200):
    imgs = []
    doc = fitz.open(stream=file_bytes, filetype='pdf')
    for page_num in range(len(doc)):
        for img in doc[page_num].get_images(full=True):
            xref = img[0]
            base = doc.extract_image(xref)
            pil  = Image.open(io.BytesIO(base['image']))
            w, h = pil.size
            if w >= min_size and h >= min_size:
                if pil.mode not in ('RGB', 'RGBA'):
                    pil = pil.convert('RGBA')
                imgs.append({
                    'image': pil,
                    'size': (w, h),
                    'page': page_num + 1
                })
    return imgs

def process_uploaded_files(uploaded_files):
    combined_text  = ''
    product_images = []
    for uf in uploaded_files:
        fname  = uf.name
        fbytes = uf.read()
        if fname.lower().endswith('.pdf'):
            text = extract_text_from_pdf(fbytes)
            if len(text.strip()) < 100:
                text = extract_text_ocr(fbytes)
            imgs = extract_images_from_pdf(fbytes)
            product_images.extend(imgs)
        elif fname.lower().endswith('.docx'):
            text = extract_text_from_docx(fbytes)
        else:
            continue
        combined_text += (
            f'\n\n{"="*60}\nSOURCE: {fname}\n{"="*60}\n\n{text}'
        )
    product_images.sort(
        key=lambda x: x['size'][0] * x['size'][1], reverse=True
    )
    return combined_text, product_images

# ─── Content generation (Anthropic SDK) ─────────────────────────────
def generate_catalog_content(combined_text, api_key):
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model='claude-sonnet-4-5',
        max_tokens=3000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                'role': 'user',
                'content': (
                    'Here are the product documents. '
                    'Generate the catalog entry.\n\n'
                    + combined_text
                )
            }
        ]
    )
    return response.content[0].text

# ─── Content parser ──────────────────────────────────────────────────
def parse_catalog(text):
    s = dict.fromkeys(
        ['product_name', 'tagline', 'usp_headline', 'usp_body',
         'competitive_edge', 'product_range', 'usage', 'tactics'], ''
    )

    def grab(pattern):
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else ''

    s['product_name']     = grab(r'PRODUCT NAME:\s*(.+?)(?:\n|$)')
    s['tagline']          = grab(r'PRODUCT TAGLINE:\s*(.+?)(?:\n|$)')
    s['usp_headline']     = grab(r'USP HEADLINE:\s*(.+?)(?:USP BODY:|\n\n|$)')
    s['usp_body']         = grab(r'USP BODY:\s*(.+?)(?=SECTION 2|$)')
    s['competitive_edge'] = grab(
        r'SECTION 2.*?COMPETITIVE EDGE.*?\n(.*?)(?=SECTION 3|$)')
    s['product_range']    = grab(
        r'SECTION 3.*?RANGE.*?\n(.*?)(?=SECTION 4|$)')
    s['usage']            = grab(
        r'SECTION 4.*?USAGE.*?\n(.*?)(?=SECTION 5|$)')
    s['tactics']          = grab(r'SECTION 5.*?TACTICS.*?\n(.*?)$')

    if not s['usp_headline'] and not s['usp_body']:
        s['usp_body'] = grab(r'SECTION 1.*?USP.*?\n(.*?)(?=SECTION 2|$)')

    return s

# ─── Color palette ───────────────────────────────────────────────────
def extract_palette(product_images):
    DEFAULT = {
        'bg':          (26, 25, 21),
        'panel':       (44, 44, 42),
        'accent':      (200, 184, 144),
        'accent_dark': (139, 90, 43),
        'text_light':  (240, 237, 230),
        'text_muted':  (160, 152, 128),
    }
    if not product_images:
        return DEFAULT
    try:
        img   = product_images[0]['image'].convert('RGB')
        small = img.resize((120, 120), Image.LANCZOS)
        q     = small.quantize(colors=10, method=2)
        raw   = q.getpalette()[:30]
        cols  = [(raw[i], raw[i+1], raw[i+2]) for i in range(0, 30, 3)]
        mid   = [c for c in cols if 40 < sum(c) / 3 < 210]
        if not mid:
            return DEFAULT

        def sat(c):
            r, g, b = c[0]/255, c[1]/255, c[2]/255
            return max(r, g, b) - min(r, g, b)

        accent      = max(mid, key=sat)
        accent_dark = tuple(max(0, int(v * 0.55)) for v in accent)
        bg          = tuple(max(0, int(v * 0.18)) for v in accent)
        panel       = tuple(max(0, int(v * 0.28)) for v in accent)

        return {
            'bg':          bg,
            'panel':       panel,
            'accent':      accent,
            'accent_dark': accent_dark,
            'text_light':  (245, 242, 236),
            'text_muted':  (180, 170, 150),
        }
    except Exception:
        return DEFAULT

# ─── Drawing helpers ─────────────────────────────────────────────────
def draw_wrapped(draw, text, x, y, max_w, font, fill, line_gap=8):
    if not text.strip():
        return y
    avg = font.size * 0.55
    cpl = max(1, int(max_w / avg))
    lines = []
    for raw in text.split('\n'):
        if raw.strip() == '':
            lines.append('')
        else:
            lines.extend(textwrap.wrap(raw.strip(), width=cpl))
    lh = font.size + line_gap
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += lh
    return y

def place_image(canvas, pil_img, x, y, max_w, max_h):
    if pil_img is None:
        return
    img = pil_img.copy()
    img.thumbnail((max_w, max_h), Image.LANCZOS)
    ox = x + (max_w - img.width)  // 2
    oy = y + (max_h - img.height) // 2
    if img.mode == 'RGBA':
        canvas.paste(img, (ox, oy), img)
    else:
        canvas.paste(img.convert('RGB'), (ox, oy))

def draw_section_header(draw, label, y_top, p):
    draw.rectangle([0, y_top, W, y_top + 56], fill=p['panel'])
    draw.rectangle([0, y_top, 6, y_top + 56], fill=p['accent'])
    draw.text((28, y_top + 14), label.upper(),
              font=fnt(22, True), fill=p['accent'])

def draw_footer(draw, name, num, p):
    draw.rectangle([0, H - 50, W, H], fill=p['panel'])
    draw.rectangle([0, H - 50, W, H - 48], fill=p['accent'])
    draw.text((28, H - 34), name.upper(),
              font=fnt(16, True), fill=p['accent'])
    draw.text((W - 60, H - 34), f'0{num}',
              font=fnt(16, True), fill=p['text_muted'])

def new_canvas(p):
    img = Image.new('RGB', (W, H), p['bg'])
    return img, ImageDraw.Draw(img)

# ─── Page renderers ──────────────────────────────────────────────────
def render_cover(sections, product_images, p):
    canvas, draw = new_canvas(p)
    draw.rectangle([0, 0, W, 8], fill=p['accent'])
    hero   = product_images[0]['image'] if product_images else None
    hero_h = int(H * 0.54)

    if hero:
        place_image(canvas, hero, 0, 8, W, hero_h - 8)
    else:
        draw.rectangle([0, 8, W, hero_h], fill=p['panel'])
        draw.text((W//2 - 80, hero_h//2 - 20), '[Product Image]',
                  font=fnt(28), fill=p['text_muted'])

    for i in range(120):
        r, g, b = p['bg']
        draw.line([(0, hero_h - 120 + i), (W, hero_h - 120 + i)],
                  fill=(r, g, b, int(255 * i / 120)))

    y = hero_h + 20
    for line in textwrap.wrap(sections['product_name'].upper(), width=24):
        draw.text((60, y), line, font=fnt(58, True), fill=p['text_light'])
        y += 70

    draw.rectangle([60, y + 10, 200, y + 16], fill=p['accent'])
    y += 40

    if sections['tagline']:
        draw.text((60, y), sections['tagline'],
                  font=fnt(26), fill=p['text_muted'])
        y += 50

    panel_y = y + 20
    draw.rectangle([60, panel_y, W - 60, H - 70], fill=p['panel'])
    draw.text((90, panel_y + 18), 'CONTENTS',
              font=fnt(16, True), fill=p['accent'])

    contents = [
        '01  Product USP',
        '02  Competitive Edge',
        '03  Product Range',
        '04  Usage Instructions',
        '05  Selling Tactics for Sales Reps',
    ]
    cy = panel_y + 56
    for item in contents:
        draw.text((90, cy), item, font=fnt(20), fill=p['text_light'])
        cy += 34

    draw_footer(draw, sections['product_name'], 0, p)
    return canvas


def render_usp(sections, product_images, p):
    canvas, draw = new_canvas(p)
    draw.rectangle([0, 0, W, 8], fill=p['accent'])
    draw_section_header(draw, '01 — Product USP', 8, p)

    hero = product_images[0]['image'] if product_images else None
    draw.rectangle([680, 80, W - 10, 640], fill=p['panel'])
    if hero:
        place_image(canvas, hero, 680, 80, 480, 560)

    y = 90
    if sections['usp_headline']:
        y = draw_wrapped(draw, sections['usp_headline'], 60, y, 580,
                         fnt(34, True), p['text_light'], 12)
        y += 16
        draw.rectangle([60, y, 160, y + 4], fill=p['accent'])
        y += 28

    y = draw_wrapped(draw, sections['usp_body'], 60, y, 580,
                     fnt(22), p['text_muted'], 10)

    nums = re.findall(
        r'(\d+\.?\d*[Xx]?\s*(?:times|x|%|X))',
        sections['usp_body'] + sections['competitive_edge'],
        re.IGNORECASE
    )[:3]
    if nums:
        bx, by = 60, 670
        for n in nums:
            draw.rounded_rectangle([bx, by, bx + 160, by + 90],
                                    radius=8, fill=p['accent_dark'])
            draw.text((bx + 12, by + 10), n.strip(),
                      font=fnt(30, True), fill=p['accent'])
            bx += 180

    draw_footer(draw, sections['product_name'], 1, p)
    return canvas


def render_competitive_edge(sections, product_images, p):
    canvas, draw = new_canvas(p)
    draw.rectangle([0, 0, W, 8], fill=p['accent'])
    draw_section_header(draw, "02 — Product's Competitive Edge", 8, p)

    raw     = sections['competitive_edge']
    bullets = [
        l.lstrip('-•* ').strip()
        for l in raw.split('\n')
        if l.strip() and len(l.strip()) > 8
    ][:4]

    side = (product_images[1]['image'] if len(product_images) > 1
            else (product_images[0]['image'] if product_images else None))
    if side:
        place_image(canvas, side, 680, 80, 480, 420)

    cy = 90
    for i, b in enumerate(bullets):
        draw.rounded_rectangle([50, cy, 650, cy + 140],
                                radius=10, fill=p['panel'])
        draw.rounded_rectangle([50, cy, 56, cy + 140],
                                radius=0, fill=p['accent'])
        draw.text((68, cy + 14), f'0{i+1}',
                  font=fnt(28, True), fill=p['accent'])
        draw_wrapped(draw, b, 120, cy + 16, 500,
                     fnt(22), p['text_light'], 8)
        cy += 160

    draw_footer(draw, sections['product_name'], 2, p)
    return canvas


def render_product_range(sections, product_images, p):
    canvas, draw = new_canvas(p)
    draw.rectangle([0, 0, W, 8], fill=p['accent'])
    draw_section_header(draw, '03 — Product Range', 8, p)

    n = min(len(product_images), 3)
    if n:
        sw = (W - 120) // n
        for i in range(n):
            ix = 60 + i * sw
            draw.rectangle([ix, 80, ix + sw - 20, 380], fill=p['panel'])
            place_image(canvas, product_images[i]['image'],
                        ix, 80, sw - 20, 300)

    cy = 400 if n else 90
    for line in sections['product_range'].split('\n'):
        s = line.strip()
        if not s or cy > H - 120:
            continue
        m = re.match(r'\*\*(.+?)\*\*\s*[—–-]?\s*(.*)', s)
        if m:
            draw.text((60, cy), '▸', font=fnt(22, True), fill=p['accent'])
            draw.text((92, cy), m.group(1).strip(),
                      font=fnt(22, True), fill=p['text_light'])
            cy += 34
            if m.group(2):
                cy = draw_wrapped(draw, m.group(2).strip(), 92, cy,
                                  W - 150, fnt(19), p['text_muted'], 6)
            cy += 12
        else:
            draw.text((60, cy), '▸', font=fnt(22, True), fill=p['accent'])
            cy = draw_wrapped(draw, s.lstrip('-•* '), 92, cy,
                              W - 150, fnt(20), p['text_light'], 6)
            cy += 10

    draw_footer(draw, sections['product_name'], 3, p)
    return canvas


def render_usage(sections, product_images, p):
    canvas, draw = new_canvas(p)
    draw.rectangle([0, 0, W, 8], fill=p['accent'])
    draw_section_header(draw, '04 — Usage Instructions', 8, p)

    hero = product_images[0]['image'] if product_images else None
    draw.rectangle([720, 80, W - 40, 640], fill=p['panel'])
    if hero:
        place_image(canvas, hero, 720, 80, 440, 560)

    steps, notes = [], []
    in_notes = False
    for line in sections['usage'].split('\n'):
        s = line.strip()
        if not s:
            continue
        if 'IMPORTANT NOTES' in s.upper():
            in_notes = True
        elif in_notes:
            notes.append(s)
        elif re.match(r'^\d+[.):]', s):
            steps.append(s)

    y = 90
    for step in steps[:6]:
        m = re.match(r'^(\d+)[.):]?\s*(.*)', step)
        num, body = (m.group(1), m.group(2)) if m else ('', step)
        cx, cy2 = 88, y + 16
        draw.ellipse([cx - 26, cy2 - 26, cx + 26, cy2 + 26],
                     fill=p['accent'])
        draw.text(
            (cx - 10 if len(num) == 1 else cx - 16, cy2 - 16),
            num, font=fnt(24, True), fill=p['bg']
        )
        draw.line([(cx, cy2 + 26), (cx, cy2 + 58)],
                  fill=p['panel'], width=3)
        ya = draw_wrapped(draw, body, 130, y, 560,
                          fnt(22), p['text_light'], 8)
        y = max(ya, y + 68) + 12

    if notes:
        y += 10
        nh = 36 + len(notes) * 32
        draw.rounded_rectangle([40, y, 680, y + nh],
                                radius=6, fill=p['panel'])
        draw.rectangle([40, y, 46, y + nh], fill=(192, 57, 43))
        draw.text((58, y + 8), 'IMPORTANT NOTES',
                  font=fnt(16, True), fill=(192, 57, 43))
        ny = y + 40
        for n in notes:
            ny = draw_wrapped(draw, '• ' + n, 58, ny, 580,
                              fnt(19), p['text_muted'], 6)

    draw_footer(draw, sections['product_name'], 4, p)
    return canvas


def render_tactics(sections, product_images, p):
    canvas, draw = new_canvas(p)
    draw.rectangle([0, 0, W, 8], fill=p['accent'])
    draw_section_header(draw, '05 — Selling Tactics for Sales Reps', 8, p)

    raw    = sections['tactics']
    blocks = re.findall(
        r'TACTIC\s+(\d+):\s*([^\n]+)\nHOW:\s*(.+?)(?=TACTIC\s+\d+|$)',
        raw, re.IGNORECASE | re.DOTALL
    )
    if not blocks:
        lines  = [l.strip() for l in raw.split('\n') if l.strip()]
        blocks = []
        i = 0
        while i < len(lines):
            tm = re.match(r'TACTIC\s*(\d+):\s*(.*)', lines[i], re.IGNORECASE)
            if tm and i + 1 < len(lines):
                hm = re.match(r'HOW:\s*(.*)', lines[i+1], re.IGNORECASE)
                blocks.append((tm.group(1), tm.group(2),
                               hm.group(1) if hm else lines[i+1]))
                i += 2
            else:
                i += 1

    for _ in range(18):
        rx, ry = random.randint(700, W - 50), random.randint(80, H - 100)
        rs     = random.randint(4, 16)
        r, g, b = p['accent']
        draw.ellipse([rx, ry, rx + rs, ry + rs], fill=(r, g, b))

    cy = 90
    for num, name, how in blocks[:5]:
        if cy + 220 > H - 60:
            break
        draw.rounded_rectangle([40, cy, W - 40, cy + 218],
                                radius=10, fill=p['panel'])
        draw.rounded_rectangle([40, cy, 52, cy + 218],
                                radius=0, fill=p['accent'])
        draw.text((68, cy + 14), f'TACTIC {num}',
                  font=fnt(14, True), fill=p['text_muted'])
        draw.text((68, cy + 36), name.strip().upper(),
                  font=fnt(22, True), fill=p['accent'])
        draw.text((68, cy + 74), 'HOW TO EXECUTE:',
                  font=fnt(14, True), fill=p['text_muted'])
        draw_wrapped(draw, how.strip(), 68, cy + 98,
                     W - 110, fnt(20), p['text_light'], 8)
        cy += 236

    draw_footer(draw, sections['product_name'], 5, p)
    return canvas


# ─── Render all pages ────────────────────────────────────────────────
def render_all_pages(sections, product_images, palette):
    return [
        ('00_cover',            render_cover(sections, product_images, palette)),
        ('01_product_usp',      render_usp(sections, product_images, palette)),
        ('02_competitive_edge', render_competitive_edge(sections, product_images, palette)),
        ('03_product_range',    render_product_range(sections, product_images, palette)),
        ('04_usage',            render_usage(sections, product_images, palette)),
        ('05_selling_tactics',  render_tactics(sections, product_images, palette)),
    ]


def pages_to_zip(pages):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, img in pages:
            img_buf = io.BytesIO()
            img.save(img_buf, 'PNG', optimize=True)
            zf.writestr(f'catalog_{name}.png', img_buf.getvalue())
    buf.seek(0)
    return buf


# ─── Streamlit UI ────────────────────────────────────────────────────
st.title('📋 Product Catalog Generator')
st.caption('Upload product documents → get a branded visual catalog in PNG format')
st.divider()

ANTHROPIC_API_KEY = st.secrets.get('ANTHROPIC_API_KEY', '')
if not ANTHROPIC_API_KEY:
    st.error(
        '❌ ANTHROPIC_API_KEY not found in Streamlit Secrets. '
        'Add it via the app Settings → Secrets panel.'
    )
    st.stop()

with st.sidebar:
    st.header('Upload Documents')
    uploaded_files = st.file_uploader(
        'Product documents (PDF or DOCX)',
        type=['pdf', 'docx'],
        accept_multiple_files=True,
        help='Upload 1-3 files: brand brief, spec sheet, product note'
    )
    if uploaded_files:
        st.success(f'{len(uploaded_files)} file(s) ready')
        for uf in uploaded_files:
            st.caption(f'• {uf.name}')

if not uploaded_files:
    st.info('👈 Upload your product documents in the sidebar to get started.')
    st.stop()

if st.button('🎨  Generate Visual Catalog', type='primary',
             use_container_width=True):

    with st.status('Running catalog pipeline...', expanded=True) as status:

        st.write('📄 Extracting text and images from documents...')
        combined_text, product_images = process_uploaded_files(uploaded_files)
        st.write(
            f'   ✅ {len(combined_text):,} characters extracted | '
            f'{len(product_images)} product images found'
        )

        st.write('🎨 Extracting brand color palette...')
        palette = extract_palette(product_images)
        st.write(f'   ✅ Accent color: RGB{palette["accent"]}')

        st.write('🤖 Generating catalog content with Claude...')
        try:
            catalog_text = generate_catalog_content(
                combined_text, ANTHROPIC_API_KEY
            )
            sections = parse_catalog(catalog_text)
            pname    = sections['product_name'] or 'Product'
            st.write(f'   ✅ Content generated for: {pname}')
        except Exception as e:
            st.error(f'Anthropic API error: {e}')
            st.stop()

        st.write('🖼️  Rendering 6 catalog pages...')
        pages = render_all_pages(sections, product_images, palette)
        st.write(f'   ✅ {len(pages)} pages rendered')

        status.update(label='Catalog ready!', state='complete')

    st.divider()
    st.subheader('📄 Catalog Preview')

    page_labels = [
        'Cover', 'USP', 'Competitive Edge',
        'Product Range', 'Usage Instructions', 'Selling Tactics'
    ]

    col1, col2 = st.columns(2)
    for i, (name, img) in enumerate(pages):
        col = col1 if i % 2 == 0 else col2
        with col:
            buf = io.BytesIO()
            img.save(buf, 'PNG')
            buf.seek(0)
            st.image(buf, caption=page_labels[i], use_container_width=True)

    st.divider()

    zip_buf = pages_to_zip(pages)
    st.download_button(
        label='⬇️  Download All Pages as ZIP',
        data=zip_buf,
        file_name=f'product_catalog_{pname.replace(" ", "_")}.zip',
        mime='application/zip',
        use_container_width=True,
        type='primary'
    )

    with st.expander('Download individual pages'):
        dl_cols = st.columns(3)
        for i, (name, img) in enumerate(pages):
            buf = io.BytesIO()
            img.save(buf, 'PNG')
            buf.seek(0)
            with dl_cols[i % 3]:
                st.download_button(
                    label=f'Page {i}: {page_labels[i]}',
                    data=buf,
                    file_name=f'catalog_{name}.png',
                    mime='image/png',
                    use_container_width=True
                )

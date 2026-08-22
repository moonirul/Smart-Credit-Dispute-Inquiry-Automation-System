import os

# ============================================================
# IMPORTANT:
# Must be set BEFORE importing paddle / paddleocr
# ============================================================

os.environ["FLAGS_enable_pir_api"] = "0"
import re
import time
import tempfile
import pymupdf
from fastapi import FastAPI, File, UploadFile, HTTPException
from paddleocr import PaddleOCR
# ============================================================
# FASTAPI
# ============================================================
app = FastAPI(
    title="Hybrid PDF OCR API",
    version="1.0.0",
)
# ============================================================
# PADDLE OCR
# ============================================================

ocr = PaddleOCR(
    lang="en",

    # Windows CPU compatibility
    enable_mkldnn=False,

    # Disable unnecessary document preprocessing
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False,
)


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize_text(text: str) -> str:
    """
    Normalize text for duplicate comparison.
    """

    text = text.strip().lower()

    # Multiple spaces -> single space
    text = re.sub(r"\s+", " ", text)

    return text


# ============================================================
# NATIVE PDF TEXT
# ============================================================

def get_native_text(page):
    """
    Extract selectable/native PDF text
    with coordinates.
    """

    words = page.get_text("words")

    items = []

    for word in words:

        if len(word) < 8:
            continue

        (
            x0,
            y0,
            x1,
            y1,
            text,
            block_no,
            line_no,
            word_no,
        ) = word

        text = str(text).strip()

        if not text:
            continue

        items.append({
            "text": text,

            "x0": float(x0),
            "y0": float(y0),
            "x1": float(x1),
            "y1": float(y1),

            "source": "native",

            "block_no": block_no,
            "line_no": line_no,
            "word_no": word_no,
        })

    return items


# ============================================================
# RENDER FULL PDF PAGE
# ============================================================

def render_page(page, output_path):
    """
    Render complete PDF page to image.

    2x resolution gives OCR more pixels.
    """

    zoom = 2.0

    matrix = pymupdf.Matrix(
        zoom,
        zoom,
    )

    pix = page.get_pixmap(
        matrix=matrix,
        alpha=False,
    )

    pix.save(output_path)

    return pix.width, pix.height


# ============================================================
# OCR FULL PAGE
# ============================================================

def ocr_full_page(
    page,
    page_number,
    temp_dir,
):
    """
    OCR the COMPLETE page.

    This is important because an image may not be exposed
    as a normal PDF image object.
    """

    image_path = os.path.join(
        temp_dir,
        f"page_{page_number}.png",
    )

    image_width, image_height = render_page(
        page,
        image_path,
    )

    # --------------------------------------------------------
    # Run PaddleOCR
    # --------------------------------------------------------

    results = ocr.predict(
        image_path
    )

    items = []

    # PDF page dimensions
    page_width = float(page.rect.width)
    page_height = float(page.rect.height)

    # Image -> PDF coordinate scale
    scale_x = page_width / image_width
    scale_y = page_height / image_height

    for result in results:

        data = result.json

        if not isinstance(data, dict):
            continue

        if "res" not in data:
            continue

        ocr_data = data["res"]

        texts = ocr_data.get(
            "rec_texts",
            [],
        )

        boxes = ocr_data.get(
            "rec_boxes",
            [],
        )

        scores = ocr_data.get(
            "rec_scores",
            [],
        )

        for index, text in enumerate(texts):

            text = str(text).strip()

            if not text:
                continue

            # ------------------------------------------------
            # Confidence
            # ------------------------------------------------

            score = None

            if index < len(scores):

                try:
                    score = float(
                        scores[index]
                    )
                except Exception:
                    score = None

            # Ignore very low-confidence OCR
            if score is not None and score < 0.30:
                continue

            # ------------------------------------------------
            # Bounding box
            # ------------------------------------------------

            if index >= len(boxes):
                continue

            box = boxes[index]

            if len(box) < 4:
                continue

            ix0, iy0, ix1, iy1 = [
                float(value)
                for value in box[:4]
            ]

            # ------------------------------------------------
            # Convert image coordinates
            # to PDF page coordinates
            # ------------------------------------------------

            x0 = ix0 * scale_x
            y0 = iy0 * scale_y

            x1 = ix1 * scale_x
            y1 = iy1 * scale_y

            items.append({
                "text": text,

                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,

                "source": "ocr",

                "score": score,
            })

    return items


# ============================================================
# DUPLICATE DETECTION
# ============================================================

def boxes_overlap(a, b):
    """
    Calculate whether two bounding boxes overlap.
    """

    ax0 = a["x0"]
    ay0 = a["y0"]
    ax1 = a["x1"]
    ay1 = a["y1"]

    bx0 = b["x0"]
    by0 = b["y0"]
    bx1 = b["x1"]
    by1 = b["y1"]

    overlap_x = max(
        0,
        min(ax1, bx1) - max(ax0, bx0),
    )

    overlap_y = max(
        0,
        min(ay1, by1) - max(ay0, by0),
    )

    overlap_area = (
        overlap_x * overlap_y
    )

    if overlap_area <= 0:
        return False

    area_a = max(
        (ax1 - ax0) * (ay1 - ay0),
        1,
    )

    area_b = max(
        (bx1 - bx0) * (by1 - by0),
        1,
    )

    smaller_area = min(
        area_a,
        area_b,
    )

    overlap_ratio = (
        overlap_area / smaller_area
    )

    return overlap_ratio >= 0.30


def remove_ocr_duplicates(
    native_items,
    ocr_items,
):
    """
    Remove OCR text that is already present
    as native PDF text.

    Uses:
    1. normalized text
    2. coordinate overlap
    """

    if not native_items:
        return ocr_items

    result = []

    native_normalized = []

    for item in native_items:

        native_normalized.append({
            "text": normalize_text(
                item["text"]
            ),
            "item": item,
        })

    for ocr_item in ocr_items:

        ocr_text = normalize_text(
            ocr_item["text"]
        )

        if not ocr_text:
            continue

        duplicate = False

        for native in native_normalized:

            native_text = native["text"]

            # ------------------------------------------------
            # Exact text match
            # ------------------------------------------------

            if ocr_text == native_text:

                if boxes_overlap(
                    ocr_item,
                    native["item"],
                ):
                    duplicate = True
                    break

            # ------------------------------------------------
            # OCR may split native text differently.
            #
            # Example:
            #
            # Native: "Invoice Number"
            # OCR:    "Invoice"
            #
            # Don't remove it unless position overlaps.
            # ------------------------------------------------

            if (
                ocr_text in native_text
                or native_text in ocr_text
            ):

                if boxes_overlap(
                    ocr_item,
                    native["item"],
                ):
                    duplicate = True
                    break

        if not duplicate:
            result.append(
                ocr_item
            )

    return result


# ============================================================
# LINE GROUPING
# ============================================================

def group_into_lines(items):
    """
    Convert positioned words into visual lines.
    """

    if not items:
        return []

    # --------------------------------------------------------
    # Sort top -> bottom
    # --------------------------------------------------------

    items = sorted(
        items,
        key=lambda item: (
            item["y0"],
            item["x0"],
        ),
    )

    lines = []

    for item in items:

        center_y = (
            item["y0"] +
            item["y1"]
        ) / 2

        height = max(
            item["y1"] -
            item["y0"],
            1,
        )

        placed = False

        for line in lines:

            tolerance = max(
                4,
                height * 0.6,
            )

            if abs(
                center_y -
                line["center_y"]
            ) <= tolerance:

                line["items"].append(
                    item
                )

                # Recalculate center
                line["center_y"] = (
                    sum(
                        (
                            x["y0"] +
                            x["y1"]
                        ) / 2
                        for x in line["items"]
                    )
                    /
                    len(line["items"])
                )

                placed = True
                break

        if not placed:

            lines.append({
                "center_y": center_y,
                "items": [item],
            })

    # --------------------------------------------------------
    # Sort lines top -> bottom
    # --------------------------------------------------------

    lines.sort(
        key=lambda line:
        line["center_y"]
    )

    output_lines = []

    for line in lines:

        # ----------------------------------------------------
        # Left -> right
        # ----------------------------------------------------

        line["items"].sort(
            key=lambda item:
            item["x0"]
        )

        texts = []

        for item in line["items"]:

            text = item["text"].strip()

            if text:
                texts.append(text)

        if texts:

            # Avoid accidental duplicate consecutive text
            clean_texts = []

            for text in texts:

                if not clean_texts:
                    clean_texts.append(text)
                    continue

                previous = normalize_text(
                    clean_texts[-1]
                )

                current = normalize_text(
                    text
                )

                if current != previous:
                    clean_texts.append(text)

            output_lines.append(
                " ".join(clean_texts)
            )

    return output_lines


# ============================================================
# EXTRACT ONE PAGE
# ============================================================

def extract_page(
    page,
    page_number,
    temp_dir,
):
    """
    Extract native + OCR text from one page.
    """

    start_time = time.perf_counter()

    print(
        f"Processing page "
        f"{page_number}..."
    )

    # --------------------------------------------------------
    # 1. Native/selectable text
    # --------------------------------------------------------

    native_items = get_native_text(
        page
    )

    print(
        f"  Native text items: "
        f"{len(native_items)}"
    )

    # --------------------------------------------------------
    # 2. OCR entire page
    # --------------------------------------------------------

    ocr_items = ocr_full_page(
        page,
        page_number,
        temp_dir,
    )

    print(
        f"  OCR text items: "
        f"{len(ocr_items)}"
    )

    # --------------------------------------------------------
    # 3. Remove OCR duplicates
    # --------------------------------------------------------

    unique_ocr_items = (
        remove_ocr_duplicates(
            native_items,
            ocr_items,
        )
    )

    print(
        f"  Unique OCR items: "
        f"{len(unique_ocr_items)}"
    )

    # --------------------------------------------------------
    # 4. Combine native + OCR
    # --------------------------------------------------------

    all_items = (
        native_items +
        unique_ocr_items
    )

    # --------------------------------------------------------
    # 5. Visual reading order
    # --------------------------------------------------------

    lines = group_into_lines(
        all_items
    )

    page_text = "\n".join(
        lines
    )

    elapsed = (
        time.perf_counter() -
        start_time
    )

    print(
        f"  Page {page_number} "
        f"completed in "
        f"{elapsed:.2f}s"
    )

    return {
        "text": page_text,
        "native_items": len(
            native_items
        ),
        "ocr_items": len(
            unique_ocr_items
        ),
        "time_seconds": round(
            elapsed,
            2,
        ),
    }


# ============================================================
# FASTAPI ROOT
# ============================================================

@app.get("/")
def root():

    return {
        "status": "ok",
        "message": "Hybrid PDF OCR API is running",
    }


# ============================================================
# OCR ENDPOINT
# ============================================================

@app.post("/ocr")
async def extract_text(
    file: UploadFile = File(...)
):

    # --------------------------------------------------------
    # Validate file
    # --------------------------------------------------------

    if not file.filename:

        raise HTTPException(
            status_code=400,
            detail="Filename is required",
        )

    if not file.filename.lower().endswith(
        ".pdf"
    ):

        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported",
        )

    request_start = time.perf_counter()

    with tempfile.TemporaryDirectory() as temp_dir:

        # ----------------------------------------------------
        # Save uploaded PDF
        # ----------------------------------------------------

        pdf_path = os.path.join(
            temp_dir,
            file.filename,
        )

        content = await file.read()

        with open(
            pdf_path,
            "wb",
        ) as f:

            f.write(content)

        # ----------------------------------------------------
        # Open PDF
        # ----------------------------------------------------

        doc = pymupdf.open(
            pdf_path
        )

        total_pages = len(doc)

        print("")
        print("=" * 60)
        print(
            f"Starting OCR: "
            f"{file.filename}"
        )
        print(
            f"Total pages: "
            f"{total_pages}"
        )
        print("=" * 60)
        print("")

        pages = []

        all_text = []

        # ----------------------------------------------------
        # IMPORTANT:
        # Process pages sequentially.
        #
        # Therefore:
        #
        # Page 1
        # Page 2
        # Page 3
        #
        # sequence is preserved.
        # ----------------------------------------------------

        for page_index, page in enumerate(
            doc
        ):

            page_number = (
                page_index + 1
            )

            result = extract_page(
                page,
                page_number,
                temp_dir,
            )

            page_text = result[
                "text"
            ]

            pages.append({
                "page": page_number,

                "text": page_text,

                "native_items":
                    result[
                        "native_items"
                    ],

                "ocr_items":
                    result[
                        "ocr_items"
                    ],

                "time_seconds":
                    result[
                        "time_seconds"
                    ],
            })

            all_text.append(
                page_text
            )

        doc.close()

        # ----------------------------------------------------
        # Full document text
        # ----------------------------------------------------

        full_text = (
            "\n\n"
            .join(all_text)
        )

        total_time = (
            time.perf_counter()
            -
            request_start
        )

        print("")
        print("=" * 60)
        print(
            f"Completed: "
            f"{file.filename}"
        )
        print(
            f"Pages: "
            f"{total_pages}"
        )
        print(
            f"Total time: "
            f"{total_time:.2f}s"
        )
        print("=" * 60)
        print("")

        return {
            "success": True,

            "filename":
                file.filename,

            "total_pages":
                total_pages,

            "processing_time_seconds":
                round(
                    total_time,
                    2,
                ),

            "pages":
                pages,

            "text":
                full_text,
        }
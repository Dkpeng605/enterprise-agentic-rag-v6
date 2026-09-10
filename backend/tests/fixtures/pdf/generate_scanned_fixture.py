"""Regenerate the committed bilingual scanned-PDF fixture.

Usage: uv run python tests/fixtures/pdf/generate_scanned_fixture.py /path/to/cjk-font
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("pass one CJK TrueType/OpenType font path")
    font_path = Path(sys.argv[1])
    if not font_path.is_file():
        raise SystemExit(f"font does not exist: {font_path}")

    image = Image.new("RGB", (1800, 500), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path), 112)
    draw.text((80, 150), "企业知识库 Enterprise RAG", font=font, fill="black")

    output = Path(__file__).with_name("scanned_zh_en.pdf")
    pdf = canvas.Canvas(str(output), pagesize=(900, 250), pageCompression=0)
    pdf.drawImage(ImageReader(image), 0, 0, width=900, height=250)
    pdf.showPage()
    pdf.save()


if __name__ == "__main__":
    main()

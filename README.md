# 🎀 Snip & Read ✂️ + File to PDF Converter

A lightweight, 100% client-side web application for Optical Character Recognition (OCR) text extraction and document-to-PDF conversion. Designed with a soft pastel aesthetic and zero server dependency—all file processing happens locally inside your browser for complete privacy.
made specifically for my girlfriend :)
---

## 🌟 Features

### ✂️ Tab 1: Snip & OCR (Image to Text)
* **100% Local & Private:** Images never leave your device. All processing runs via browser Web Workers.
* **Interactive Canvas Snipping Tool:** Click and drag a selection box to crop and extract text from a single word, sentence, or column.
* **Multi-File Batch Queue:** Process multiple photos at once, switch active thumbnails, or extract text across all uploaded images sequentially.
* **Zoom & Pan Controls:** Scroll/slider zoom (up to 400%) with viewport panning for precise cropping on high-resolution or small-text documents.
* **Text Cleanup Regex:** Toggleable noise removal to filter out stray OCR artifacts, symbols, and dark shadow borders.
* **Fullscreen Proofreading View:** Side-by-side modal displaying the original cropped image alongside an editable text area with dynamic font sizing.
* **Multi-Format Export:** Copy to clipboard, download as `.txt`, or export as formatted `.pdf`.

### 📄 Tab 2: File-to-PDF Converter
* **Supported File Types:** Images (`.png`, `.jpg`, `.webp`, `.bmp`) and Documents (`.docx`, `.txt`).
* **Client-Side Document Parsing:** Uses Mammoth.js for text extraction and jsPDF for page rendering.
* **Merge & Customize:** Reorder queued files, adjust page size (`A4`, `Letter`), orientation (`Portrait`, `Landscape`), and margins, then convert files individually or merge them into a single multi-page PDF.

---

## 🛠️ Tech Stack

* **Frontend:** HTML5, Tailwind CSS, Google Fonts (*Fredoka*, *Quicksand*)
* **OCR Engine:** [Tesseract.js v5.1.1](https://github.com/naptha/tesseract.js)
* **Document Parsing:** [Mammoth.js v1.6.0](https://github.com/mwilliamson/mammoth.js)
* **PDF Generation:** [jsPDF v2.5.1](https://github.com/parallax/jsPDF)

---

## 🚀 How to Add This to Your Repository

1. Open your project folder in **VS Code**.
2. Create a new file named **`README.md`**.
3. Copy and paste the code block above into `README.md` and save the file.
4. Open your VS Code terminal and run these 3 quick commands to push it to GitHub:

   ```powershell
   git add README.md
   git commit -m "Add README documentation"
   git push origin main

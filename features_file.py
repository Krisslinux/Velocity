from pathlib import Path
from typing import List

from utils import zip_paths, unzip_to_dir, text_to_pdf, images_to_pdf, merge_pdfs, safe_filename

def do_zip(input_paths: List[Path], out_zip: Path) -> Path:
    return zip_paths(input_paths, out_zip)

def do_unzip(zip_file: Path, out_dir: Path) -> Path:
    return unzip_to_dir(zip_file, out_dir)

def do_rename(file_path: Path, new_name: str) -> Path:
    new_name = safe_filename(new_name)
    if not new_name:
        raise ValueError("New filename is empty.")
    target = file_path.with_name(new_name)
    file_path.rename(target)
    return target

def do_text_to_pdf(text: str, out_pdf: Path) -> Path:
    return text_to_pdf(text, out_pdf)

def do_images_to_pdf(images: List[Path], out_pdf: Path) -> Path:
    return images_to_pdf(images, out_pdf)

def do_merge_pdfs(pdfs: List[Path], out_pdf: Path) -> Path:
    return merge_pdfs(pdfs, out_pdf)

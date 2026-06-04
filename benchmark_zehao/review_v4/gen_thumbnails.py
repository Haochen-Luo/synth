import os
import glob
from PIL import Image

def main():
    directory = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(directory):
        print(f"Directory {directory} does not exist.")
        return
        
    png_files = glob.glob(os.path.join(directory, "*.png"))
    print(f"Found {len(png_files)} PNG files to process.")
    
    processed = 0
    for png_path in sorted(png_files):
        base = os.path.basename(png_path)
        name, _ = os.path.splitext(base)
        
        # Output filename
        jpg_path = os.path.join(directory, f"{name}_thumb.jpg")
        
        try:
            with Image.open(png_path) as img:
                # Calculate new size (30%)
                w, h = img.size
                new_w = max(1, int(w * 0.3))
                new_h = max(1, int(h * 0.3))
                
                # Resampling filter resolution compatible with different Pillow versions
                try:
                    resample_filter = Image.Resampling.LANCZOS
                except AttributeError:
                    try:
                        resample_filter = Image.LANCZOS
                    except AttributeError:
                        resample_filter = Image.ANTIALIAS
                
                # Convert RGBA to RGB for JPEG compatibility
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    if img.mode == "RGBA":
                        background.paste(img, mask=img.split()[3])
                    else:
                        background.paste(img)
                    img_rgb = background
                else:
                    img_rgb = img.convert("RGB")
                    
                thumb = img_rgb.resize((new_w, new_h), resample=resample_filter)
                thumb.save(jpg_path, "JPEG", quality=85)
                
                print(f"Generated: {os.path.basename(jpg_path)} ({w}x{h} -> {new_w}x{new_h})")
                processed += 1
        except Exception as e:
            print(f"Error processing {base}: {e}")
            
    print(f"Successfully processed {processed}/{len(png_files)} files.")

if __name__ == "__main__":
    main()

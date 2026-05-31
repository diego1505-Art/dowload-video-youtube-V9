import os
import subprocess
import shutil

def fix_videos(directory):
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".mp4") and not file.startswith("fixed_"):
                input_path = os.path.join(root, file)
                output_path = os.path.join(root, f"fixed_{file}")
                
                print(f"Fixing {file}...")
                try:
                    # On tente de remuxer proprement
                    cmd = [
                        'ffmpeg', '-y', '-i', input_path, 
                        '-c', 'copy', '-map', '0:v', '-map', '0:a', 
                        output_path
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    
                    if result.returncode == 0:
                        # Si ça a marché, on remplace l'ancien par le nouveau
                        os.remove(input_path)
                        os.rename(output_path, input_path)
                        print(f"✓ {file} réparé.")
                    else:
                        if os.path.exists(output_path):
                            os.remove(output_path)
                        print(f"✗ Impossible de réparer {file} (fichier peut-être corrompu).")
                except Exception as e:
                    print(f"Error fixing {file}: {e}")

if __name__ == "__main__":
    target_dir = "downloads"
    if os.path.exists(target_dir):
        fix_videos(target_dir)
        print("\nRéparation terminée !")
    else:
        print("Dossier downloads introuvable.")

"""Convierte el logo existente a formatos de navegador sin redibujarlo.

Requiere Pillow solo para generar assets, no en la aplicación Django.
"""
from pathlib import Path
from PIL import Image

folder = Path(__file__).resolve().parents[1] / "incidents/static/incidents/images"
source = Image.open(folder / "secuoya-chile-logo.png").convert("RGBA")
# Descartar únicamente margen transparente y píxeles casi invisibles.
bounds = source.getchannel("A").point(lambda alpha: 255 if alpha >= 32 else 0).getbbox()
logo = source.crop(bounds)
logo.save(folder / "secuoya-chile-wordmark.png", optimize=True)
# Recorte exacto de la S del archivo existente, autorizado por el usuario.
# Coordenadas sobre el wordmark sin margen: la siguiente letra empieza en x=222.
symbol = logo.crop((0, 0, 220, 270))
symbol = symbol.crop(symbol.getchannel("A").point(lambda a: 255 if a >= 32 else 0).getbbox())
symbol.thumbnail((220, 220), Image.Resampling.LANCZOS)
canvas = Image.new("RGBA", (256, 256), "white")
canvas.alpha_composite(symbol, ((256 - symbol.width) // 2, (256 - symbol.height) // 2))
canvas.save(folder / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
canvas.resize((32, 32), Image.Resampling.LANCZOS).save(folder / "favicon-32.png")
canvas.resize((180, 180), Image.Resampling.LANCZOS).save(folder / "apple-touch-icon.png")
print("Wordmark íntegro y favicon con recorte autorizado de la S; original preservado.")

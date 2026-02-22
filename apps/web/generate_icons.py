"""Generate PWA icons at 192x192 and 512x512 for install prompt."""
from PIL import Image, ImageDraw

INDIGO = (79, 70, 229)  # #4F46E5
WHITE = (255, 255, 255)

def draw_icon(size):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded rectangle background
    r = size // 5  # corner radius
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=INDIGO)

    # 3D cube (isometric) - scaled to icon size
    cx, cy = size * 0.5, size * 0.44
    s = size * 0.27  # half-size of cube

    # Cube faces
    top = [(cx, cy - s), (cx - s, cy - s * 0.5), (cx, cy), (cx + s, cy - s * 0.5)]
    left = [(cx, cy), (cx - s, cy - s * 0.5), (cx - s, cy + s * 0.5), (cx, cy + s)]
    right = [(cx, cy), (cx + s, cy - s * 0.5), (cx + s, cy + s * 0.5), (cx, cy + s)]

    draw.polygon(top, fill=(255, 255, 255, 160))
    draw.polygon(left, fill=(255, 255, 255, 110))
    draw.polygon(right, fill=(255, 255, 255, 80))

    # Slice lines across cube
    lw = max(1, size // 200)
    for frac in [0.33, 0.55, 0.77]:
        y = cy - s * 0.5 + s * frac * 2
        x_off = s * (1 - abs(frac - 0.5))
        draw.line([(cx - s, y), (cx + s, y)], fill=INDIGO, width=lw)

    # Build plate
    plate_y = cy + s + size * 0.06
    plate_h = size * 0.03
    draw.rounded_rectangle(
        [cx - s - size * 0.02, plate_y, cx + s + size * 0.02, plate_y + plate_h],
        radius=max(1, size // 128),
        fill=(255, 255, 255, 230)
    )

    # "U1" text at bottom
    text_y = size * 0.74
    text_size = size * 0.13
    # Use simple rectangle-based "U1" since we can't rely on fonts
    # Letter U
    u_x = cx - size * 0.14
    u_w = size * 0.09
    u_h = size * 0.12
    bar = max(2, size // 80)
    draw.rectangle([u_x, text_y, u_x + bar, text_y + u_h], fill=(255, 255, 255, 220))
    draw.rectangle([u_x + u_w, text_y, u_x + u_w + bar, text_y + u_h], fill=(255, 255, 255, 220))
    draw.rectangle([u_x, text_y + u_h - bar, u_x + u_w + bar, text_y + u_h], fill=(255, 255, 255, 220))

    # Letter 1
    one_x = cx + size * 0.06
    draw.rectangle([one_x, text_y, one_x + bar, text_y + u_h], fill=(255, 255, 255, 220))
    draw.rectangle([one_x - bar, text_y, one_x + bar, text_y + bar], fill=(255, 255, 255, 220))

    return img

for size in [192, 512]:
    img = draw_icon(size)
    img.save(f'icon-{size}.png')
    print(f'Generated icon-{size}.png')

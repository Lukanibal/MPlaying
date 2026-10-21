import sys

from PIL import Image, ImageDraw


def create_icon(path):
    image = Image.new("RGBA", (256, 256), "#10211e")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, 244, 244), radius=52, fill="#15352e")
    draw.ellipse((38, 38, 218, 218), fill="#09110f", outline="#73e0a1", width=8)
    draw.ellipse((60, 60, 196, 196), outline="#29473e", width=4)
    draw.ellipse((78, 78, 178, 178), outline="#29473e", width=3)
    draw.ellipse((102, 102, 154, 154), fill="#73e0a1")
    draw.rounded_rectangle((147, 55, 165, 143), radius=8, fill="#f5fff9")
    draw.rounded_rectangle((111, 80, 164, 98), radius=8, fill="#f5fff9")
    draw.ellipse((94, 127, 125, 153), fill="#f5fff9")
    draw.ellipse((132, 118, 163, 144), fill="#f5fff9")
    image.save(path)


if __name__ == "__main__":
    create_icon(sys.argv[1])
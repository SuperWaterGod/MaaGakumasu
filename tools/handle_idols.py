import os
import io
import sys
from PIL import Image

# --- 配置参数 ---
INPUT_FOLDER = "input"
OUTPUT_FOLDER = "output"  # 处理后的图片将保存到这个新文件夹
TARGET_SIZE = (140, 190)        # 目标宽度和高度 (像素)
TARGET_DPI = (96, 96)           # 水平DPI, 垂直DPI


if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
# --- 解决 UnicodeEncodeError 结束 ---


def process_images():
    print(f"开始处理图片，输入文件夹: '{INPUT_FOLDER}'")
    print(f"处理后的图片将保存到: '{OUTPUT_FOLDER}'")
    print(f"目标尺寸: {TARGET_SIZE[0]}x{TARGET_SIZE[1]} 像素, DPI: {TARGET_DPI[0]}")

    # 1. 创建输出文件夹（如果不存在）
    if not os.path.exists(OUTPUT_FOLDER):
        try:
            os.makedirs(OUTPUT_FOLDER)
            print(f"已创建输出文件夹: '{OUTPUT_FOLDER}'")
        except OSError as e:
            print(f"错误：无法创建输出文件夹 '{OUTPUT_FOLDER}': {e}")
            return

    # 2. 检查输入文件夹是否存在
    if not os.path.exists(INPUT_FOLDER):
        print(f"错误：输入文件夹 '{INPUT_FOLDER}' 未找到。请确保它在脚本的同级目录下并包含PNG文件。")
        return

    processed_count = 0
    skipped_count = 0

    # 3. 遍历输入文件夹中的所有文件
    for filename in os.listdir(INPUT_FOLDER):
        # 4. 检查文件是否是PNG格式
        if filename.lower().endswith(".png"):
            input_filepath = os.path.join(INPUT_FOLDER, filename)
            output_filepath = os.path.join(OUTPUT_FOLDER, filename)

            try:
                # 打开图片
                img = Image.open(input_filepath)

                # 5. 保持透明背景并转换为32位色深
                # Pillow的'RGBA'模式表示Red, Green, Blue, Alpha (透明度) 四个通道，
                # 每个通道8位，总共32位。这确保了透明度被保留。
                if img.mode != 'RGBA':
                    img = img.convert('RGBA')

                # 6. 缩放图片
                # Image.Resampling.LANCZOS 是一个高质量的重采样滤波器，适用于缩放
                img_resized = img.resize(TARGET_SIZE, Image.Resampling.LANCZOS)

                # 7. 保存图片，并设置DPI
                # 'dpi'参数用于设置图片文件的DPI元数据
                img_resized.save(output_filepath, dpi=TARGET_DPI)

                print(f"  成功处理: '{filename}'")
                processed_count += 1

            except Exception as e:
                print(f"  错误处理 '{filename}': {e}")
                skipped_count += 1
        else:
            print(f"  跳过非PNG文件: '{filename}'")

    print(f"\n图片处理完成！")
    print(f"总共找到PNG文件: {processed_count + skipped_count}")
    print(f"成功处理: {processed_count}")
    print(f"失败或跳过: {skipped_count}")
    if processed_count == 0 and skipped_count == 0:
        print(f"在 '{INPUT_FOLDER}' 文件夹中没有找到任何PNG图片。")


if __name__ == "__main__":
    process_images()

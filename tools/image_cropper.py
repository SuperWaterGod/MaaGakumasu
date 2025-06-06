import os
from PIL import Image
from pathlib import Path


def crop_and_save_images():
    # 1. 定义输入和输出文件夹路径
    current_dir = Path.cwd()  # 获取当前工作目录
    input_folder = current_dir / "input"
    output_folder = current_dir / "output"

    # 2. 检查输入文件夹是否存在
    if not input_folder.is_dir():
        print(f"错误：输入文件夹 '{input_folder}' 不存在。")
        return

    # 3. 创建输出文件夹 (如果不存在)
    output_folder.mkdir(parents=True, exist_ok=True)
    print(f"输出文件夹 '{output_folder}' 已准备好。")

    # 4. 定义裁剪参数
    # (x, y) 是左上角坐标
    crop_x_start = 53
    crop_y_start = 661
    crop_width = 614
    crop_height = 283

    # Pillow的crop方法需要 (left, upper, right, lower)
    # left = x_start
    # upper = y_start
    # right = x_start + width
    # lower = y_start + height
    crop_box = (
        crop_x_start,
        crop_y_start,
        crop_x_start + crop_width,
        crop_y_start + crop_height
    )

    # 5. 遍历输入文件夹中的所有PNG图片
    image_files_processed = 0
    for image_file in input_folder.glob("*.png"):
        try:
            print(f"正在处理: {image_file.name} ...")

            # 打开图片
            img = Image.open(image_file)

            # 确保图片是 RGBA 模式 (32位色深)
            # PNG本身就可以支持32位RGBA，如果原图是RGB，转换一下
            if img.mode != "RGBA":
                img = img.convert("RGBA")
                print(f"  已将 {image_file.name} 转换为 RGBA 模式。")

            # 裁剪图片
            cropped_img = img.crop(crop_box)

            # 定义输出文件路径
            output_file_path = output_folder / image_file.name

            # 保存裁剪后的图片，设置DPI
            # PNG格式本身不强制包含DPI信息，但Pillow的save方法支持写入DPI
            # "色深32bit" 通常指 RGBA (8位*4通道 = 32位)
            cropped_img.save(output_file_path, format="PNG", dpi=(96, 96))

            print(f"  已裁剪并保存到: {output_file_path}")
            image_files_processed += 1

        except Exception as e:
            print(f"处理图片 {image_file.name} 时发生错误: {e}")

    if image_files_processed == 0:
        print(f"在 '{input_folder}' 中没有找到PNG图片。")
    else:
        print(f"\n处理完成！共处理了 {image_files_processed} 张图片。")


if __name__ == "__main__":
    crop_and_save_images()

import os
from PIL import Image


def process_images_in_directory(input_dir, output_dir="output", target_width=148, output_format="PNG", dpi=(96, 96)):
    """
    批量处理指定目录下的所有图片。

    Args:
        input_dir (str): 包含待处理图片的输入目录路径。
        output_dir (str, optional): 保存处理后图片的输出目录路径。
                                    如果不存在，则会自动创建。默认为 "processed_images"。
        target_width (int, optional): 目标宽度（像素）。默认为 148。
        output_format (str, optional): 输出图片的格式（例如 "PNG", "JPEG"）。默认为 "PNG"。
        dpi (tuple, optional): 输出图片的 DPI（水平DPI, 垂直DPI）。默认为 (96, 96)。
    """

    if not os.path.isdir(input_dir):
        print(f"错误：输入目录 '{input_dir}' 不存在。")
        return

    # 创建输出目录（如果不存在）
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"已创建输出目录: '{output_dir}'")

    processed_count = 0
    for filename in os.listdir(input_dir):
        filepath = os.path.join(input_dir, filename)

        # 检查是否是文件并且不是目录
        if os.path.isfile(filepath):
            try:
                # 打开图片
                img = Image.open(filepath)

                # 1. 保持比例不变，宽为 148px
                original_width, original_height = img.size
                if original_width > 0:  # 避免除以零
                    aspect_ratio = original_height / original_width
                    new_width = target_width
                    new_height = int(new_width * aspect_ratio)

                    # 调整图片大小
                    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)  # 使用LANCZOS进行高质量缩放

                    # 2. 图片格式为 png
                    # 3. 96dpi
                    # 保存图片时指定格式和DPI
                    base_name, _ = os.path.splitext(filename)
                    output_filename = f"{base_name}.{output_format.lower()}"
                    output_filepath = os.path.join(output_dir, output_filename)

                    img.save(output_filepath, format=output_format, dpi=dpi)
                    print(f"处理并保存: {filename} -> {output_filename}")
                    processed_count += 1
                else:
                    print(f"警告: 跳过文件 '{filename}', 宽度为0。")

            except Exception as e:
                print(f"处理文件 '{filename}' 时发生错误: {e}")

    print(f"\n所有图片处理完毕。共处理了 {processed_count} 张图片。")


if __name__ == "__main__":

    input_directory = 'input'
    process_images_in_directory(input_directory)

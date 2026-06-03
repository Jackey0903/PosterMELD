import io
import os
import requests
import json
import base64
from PIL import Image
from typing import Optional, Tuple

class ImageTools:
    """
    图像操作工具类，封装基于 nanobanana/qwen-image 或者 gemini-2.5-flash-image 的视觉能力，
    同时也混合了针对本地底层预处理的基础工具（如 Pillow 的裁剪与缩放）。
    """
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, model: Optional[str] = None):
        """
        初始化视觉工具
        :param api_key: 视觉服务 API 密钥
        :param base_url: 视觉服务网关的请求基础路径
        :param model: 默认使用的模型名称
        """
        self.api_key = api_key or os.getenv("IMAGE_API_KEY") or os.getenv("VLM_API_KEY")
        self.base_url = base_url or os.getenv("IMAGE_BASE_URL") or os.getenv("VLM_BASE_URL")
        self.model = model or os.getenv("IMAGE_MODEL") or "gemini-2.5-flash-image"

    def generate_image(self, prompt: str, width: int = 1024, height: int = 1024, output_path: str = "generated_img.png") -> str:
        """
        使用配置好的模型和 API 获取图像，并保存到本地。
        由于通常的标准接口走的是 OpenAI 风格的 /images/generations 路径：
        Returns:
            生成的图片对应的本地文件路径
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        # 基于您的代理网关针对 gemini-2.5-flash-image 的底层实现逻辑，
        # 它被映射到了标准的 OpenAI 对话补全端点，并在回复中以 Markdown `![image](data:image/png;base64,...)` 形式返还图像数据。
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}]
        }
        
        print(f"正在调用图像生成 API (模型: {self.model})，端点 /chat/completions，提示词: {prompt}...")
        
        try:
            if not self.api_key or not self.base_url:
                raise ValueError("IMAGE_API_KEY/VLM_API_KEY and IMAGE_BASE_URL/VLM_BASE_URL are required for image generation")
            response = requests.post(url, headers=headers, json=payload, timeout=90)
            response.raise_for_status()
            
            data = response.json()
            if "choices" in data and len(data["choices"]) > 0:
                content = data["choices"][0]["message"]["content"]
                
                # 提取 markdown 里面的 base64 图片数据包: ![image](data:image/...;base64,xxxx)
                import re
                match = re.search(r"data:image/[^;]+;base64,([^)]+)", content)
                if match:
                    b64_str = match.group(1)
                    img_bytes = base64.b64decode(b64_str)
                    
                    with open(output_path, "wb") as f:
                        f.write(img_bytes)
                    print(f"图像生成成功，路径: {output_path}")
                    return output_path
                else:
                    raise Exception(f"API 回复中未能提取到合法的 Base64 Markdown 图片标签。返回的原始数据为:\n{content[:200]}...")
            else:
                raise Exception(f"API 返回数据结构异常: {data}")
        except Exception as e:
            print(f"生成图像失败，可能因为网络或者服务接口变更错误: {e}")
            print("回退生成灰色占位图...")
            img = Image.new("RGB", (width, height), color=(200, 200, 200))
            img.save(output_path)
            return output_path

    def edit_image(self, image_path: str, prompt: str, output_path: str = "edited_img.png") -> str:
        """
        根据提示词，通过视觉 API 编辑目标图像。
        Returns:
            编辑后的图像的本地保存路径
        """
        import base64
        import re
        
        print(f"正在请求视觉 API 编辑图像 {image_path}，提示词: {prompt}，端点 /chat/completions...")
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        url = f"{self.base_url}/chat/completions"
        
        try:
            if not self.api_key or not self.base_url:
                raise ValueError("IMAGE_API_KEY/VLM_API_KEY and IMAGE_BASE_URL/VLM_BASE_URL are required for image editing")
            # 将原图转为 Base64 以多模态方式发送
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")
                
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt + " 请只输出编辑后的图片结果。"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{img_b64}"
                                }
                            }
                        ]
                    }
                ]
            }
            
            response = requests.post(url, headers=headers, json=payload, timeout=90)
            response.raise_for_status()
            
            data = response.json()
            if "choices" in data and len(data["choices"]) > 0:
                content = data["choices"][0]["message"]["content"]
                
                # 兼容旧逻辑，通过更健壮的正则表达式提取 Base64
                match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\n]+)", content)
                if match:
                    b64_str = match.group(1)
                    img_bytes = base64.b64decode(b64_str)
                    
                    with open(output_path, "wb") as f:
                        f.write(img_bytes)
                    print(f"图像编辑成功: {output_path}")
                    return output_path
                else:
                    raise Exception(f"未能在结果中提取到 Base64 图像: {content[:200]}...")
            else:
                raise Exception(f"API 返回数据结构异常: {data}")
                
        except Exception as e:
            print(f"编辑图像服务请求失败，直接返回原图: {e}")
            return image_path

    def crop_and_resize(self, image_path: str, target_width: int, target_height: int, output_path: str) -> str:
        """
        本地预处理后备方案：强行裁剪和按比例缩放图像，确保其绝对符合布局中的 (宽度 x 高度)
        保持了图片的纵横比不变，通过中间裁剪居中处理对齐。
        """
        with Image.open(image_path) as img:
            img_aspect = img.width / img.height
            target_aspect = target_width / target_height
            
            if img_aspect > target_aspect:
                # 原图片比目标的要“扁宽”，所以砍掉两边的宽
                new_width = int(img.height * target_aspect)
                left = (img.width - new_width) / 2
                img = img.crop((left, 0, left + new_width, img.height))
            else:
                # 原图片比目标的要“高瘦”，所以砍掉上下的高
                new_height = int(img.width / target_aspect)
                top = (img.height - new_height) / 2
                img = img.crop((0, top, img.width, top + new_height))
                
            img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
            img.save(output_path)
            
        return output_path

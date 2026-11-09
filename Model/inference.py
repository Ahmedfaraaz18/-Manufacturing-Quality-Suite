"""Reference preprocessing and frozen classifier. No web application logic."""
from pathlib import Path
import numpy as np
from PIL import Image, ImageOps
MODEL_VERSION = "BIT-PLATE-CV-1.0"
CLASS_NAMES = ("Normal", "Defective")

def features(image):
    image = ImageOps.exif_transpose(image).convert("RGB")
    image = image.resize((128, 128), Image.Resampling.BILINEAR)
    a = np.asarray(image.convert("L"), dtype=np.float64)[16:112,16:112] / 255.0
    residual = a - np.median(a)
    return np.array([a.mean(), a.std(), *np.quantile(residual,[.01,.05,.1,.25,.75,.9,.95,.99]),
        *[(residual < -v).mean() for v in [.03,.06,.1,.15,.2]],
        np.abs(np.diff(a,axis=0)).mean(), np.abs(np.diff(a,axis=1)).mean()], dtype=np.float64)

def predict(image_path, model_path=None):
    path = Path(model_path) if model_path else Path(__file__).with_name("classifier.npz")
    with np.load(path, allow_pickle=False) as model:
        x = features(Image.open(image_path))
        z = float(((x-model["mean"])/model["scale"]) @ model["coef"] + model["intercept"].item())
    probability = float(1 / (1 + np.exp(-np.clip(z,-50,50))))
    index = int(probability >= .5)
    return {"predicted_class": CLASS_NAMES[index], "confidence": probability if index else 1-probability,
            "defect_probability": probability, "model_version": MODEL_VERSION}

if __name__ == "__main__":
    import argparse, json
    parser=argparse.ArgumentParser()
    parser.add_argument("image")
    args=parser.parse_args()
    print(json.dumps(predict(args.image),indent=2))

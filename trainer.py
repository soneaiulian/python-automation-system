import pandas as pd
import joblib
import numpy as np
import os

from sklearn.ensemble import RandomForestClassifier
from sklearn.utils import class_weight
from sklearn.metrics import classification_report

print("📥 Loading dataset...")

# =========================
# LOAD DATASET
# =========================
df = pd.read_csv("data/training_data_gold.csv")

# 🔥 FIX IMPORTANT (coloane corecte)
df.columns = df.columns.astype(str)

print("🧠 Columns:")
print(df.columns)

if len(df) < 500:
    print("❌ Prea puține date")
    exit()

# =========================
# SPLIT X / Y
# =========================
X = df.drop(columns=["target", "time"], errors="ignore")

# 🔥 PROTECȚIE (dacă nu există target)
if "target" not in df.columns:
    print("❌ Lipseste coloana target")
    exit()

y = df["target"]

# =========================
# CLEAN
# =========================
X = X.replace([np.inf, -np.inf], 0)
X = X.fillna(0)

# 🔥 FORȚĂM FEATURE NAMES CORECTE
X.columns = X.columns.astype(str)

# =========================
# CLASS WEIGHTS
# =========================
weights = class_weight.compute_class_weight(
    class_weight='balanced',
    classes=np.unique(y),
    y=y
)

class_weights = {i: weights[i] for i in range(len(weights))}

# =========================
# MODEL
# =========================
model = RandomForestClassifier(
    n_estimators=400,
    max_depth=14,
    min_samples_split=8,
    min_samples_leaf=4,
    class_weight=class_weights,
    random_state=42,
    n_jobs=-1
)

print("🧠 Training model...")
model.fit(X, y)

# =========================
# REPORT
# =========================
y_pred = model.predict(X)
print("\n📊 TRAIN REPORT:")
print(classification_report(y, y_pred))

# =========================
# SAVE
# =========================
os.makedirs("models", exist_ok=True)

joblib.dump(model, "models/model_gold.pkl")
joblib.dump(list(X.columns), "models/features_gold.pkl")

print("✅ MODEL GOLD SALVAT")
print(f"📊 Features: {len(X.columns)}")
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score
import joblib

# load dataset
df = pd.read_csv("data/dataset_train.csv")

# convert regime (text → numeric)
df["regime"] = df["regime"].map({
    "TREND_BULLISH": 1,
    "TREND_BEARISH": -1,
    "RANGE": 0,
    "HIGH_VOLATILITY": 2
})

# features / target
X = df.drop("target", axis=1)
y = df["target"]

# split
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42
)

# model
model = RandomForestClassifier(n_estimators=100)

model.fit(X_train, y_train)

# test
preds = model.predict(X_test)
acc = accuracy_score(y_test, preds)

print("Model Accuracy:", acc)

# save model
joblib.dump(model, "aurora_model.pkl")

print("Model saved as aurora_model.pkl")
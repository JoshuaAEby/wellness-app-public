const LABEL_COLOR = {
  High: "#10b981",
  Moderate: "#eab308",
  Low: "#f97316",
  "Rest Day": "#ef4444",
};

export default function ReadinessRing({ score, label, size = 160 }) {
  const r = 68;
  const cx = 80;
  const cy = 80;
  const circumference = 2 * Math.PI * r;
  const arcLength = circumference * 0.75; // 270° arc
  const progress = score != null ? (score / 100) * arcLength : 0;
  const color = LABEL_COLOR[label] ?? "#6366f1";
  const scale = size / 160;

  return (
    <div className="relative flex items-center justify-center" style={{ width: size, height: size }}>
      {/* Ring rotated so gap is at bottom */}
      <svg width={size} height={size} viewBox="0 0 160 160" style={{ transform: "rotate(135deg)" }}>
        {/* Track */}
        <circle
          cx={cx} cy={cy} r={r}
          fill="none" stroke="#1e293b" strokeWidth="14"
          strokeDasharray={`${arcLength} ${circumference - arcLength}`}
          strokeLinecap="round"
        />
        {/* Progress */}
        <circle
          cx={cx} cy={cy} r={r}
          fill="none" stroke={color} strokeWidth="14"
          strokeDasharray={`${progress} ${circumference - progress}`}
          strokeLinecap="round"
          style={{ transition: "stroke-dasharray 0.6s ease" }}
        />
      </svg>
      {/* Center text — counter-rotated */}
      <div className="absolute flex flex-col items-center">
        <span style={{ fontSize: Math.round(36 * scale), fontWeight: 700, color: "white", lineHeight: 1.1 }}>
          {score != null ? Math.round(score) : "—"}
        </span>
        <span style={{ fontSize: Math.max(10, Math.round(12 * scale)), fontWeight: 500, color }}>
          {label ?? "No data"}
        </span>
      </div>
    </div>
  );
}

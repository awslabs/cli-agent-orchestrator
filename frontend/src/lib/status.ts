const STATUS_LABELS: Record<string, string> = {
  idle: "空闲",
  processing: "处理中",
  completed: "已完成",
  waiting_user_answer: "等待用户输入",
  error: "异常",
};

export function toStatusLabel(status: string | undefined): string {
  if (!status) {
    return "未知";
  }
  const normalized = status.toLowerCase();
  return STATUS_LABELS[normalized] || status;
}

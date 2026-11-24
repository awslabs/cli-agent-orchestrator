export const CAO_SERVER_PORT: number;
export function portAnswers(port: number, timeoutMs?: number): Promise<boolean>;
export function pickFreePort(avoid?: number): Promise<number>;
export function resolveDemoPort(
  env?: Record<string, string | undefined>,
  caoPort?: number,
): Promise<number>;

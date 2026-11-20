export interface ShippedPackage {
  name: string;
  version: string;
}
export function owningPackage(moduleId: string): { dir: string; name: string } | null;
export function virtualOwner(id: string): string;
export function externalPackages(external: unknown, projectRoot: string): ShippedPackage[];
export function collectPackages(
  moduleIds: Iterable<string>,
  projectRoot: string,
  extra?: ShippedPackage[],
): ShippedPackage[];
export function writeBundledPackages(options: {
  projectRoot: string;
  project: string;
  config: string;
  packages: ShippedPackage[];
  outputDir?: string | null;
  outputs?: string[];
}): string;
export function bundledPackagesPlugin(options?: {
  project?: string;
  config?: string;
  projectRoot?: string;
}): import("vite").Plugin;
export function docusaurusClientPackagesPlugin(context: { siteDir: string }): {
  name: string;
  configureWebpack: (config: unknown, isServer: boolean) => Record<string, unknown>;
};

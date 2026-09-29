/** Minimal shape of VS Code's WorkspaceConfiguration inspection API. */
export interface InspectableConfiguration {
  inspect<T>(section: string): {
    defaultValue?: T;
    globalValue?: T;
    workspaceValue?: T;
    workspaceFolderValue?: T;
  } | undefined;
}

/**
 * Read only defaults and user-global values for settings that affect process
 * execution. Workspace-provided values are intentionally ignored because a
 * cloned repository must not be able to choose an executable or Python file.
 */
export function trustedUserSetting<T>(
  config: InspectableConfiguration,
  key: string,
  fallback: T,
): T {
  const inspected = config.inspect<T>(key);
  if (inspected?.globalValue !== undefined) return inspected.globalValue;
  if (inspected?.defaultValue !== undefined) return inspected.defaultValue;
  return fallback;
}

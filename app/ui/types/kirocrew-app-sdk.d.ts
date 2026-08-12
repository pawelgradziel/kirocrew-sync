// Ambient type declarations for @kirocrew/app-sdk (see website/src/app-sdk/index.ts
// in the kirocrew repo). Editor/type-check convenience only — resolved through
// the host's import map at runtime, never bundled. Narrowed to what this app uses.
declare module '@kirocrew/app-sdk' {
  export interface AppApi {
    get<T = unknown>(path: string, init?: RequestInit): Promise<T>;
    post<T = unknown>(path: string, body?: unknown): Promise<T>;
    put<T = unknown>(path: string, body?: unknown): Promise<T>;
    patch<T = unknown>(path: string, body?: unknown): Promise<T>;
    del<T = unknown>(path: string): Promise<T>;
  }

  export interface AppPermissions {
    api: string[];
    events: string[];
  }

  export interface AppInfo {
    name: string;
    version: string;
    permissions: AppPermissions;
  }

  export interface AppTheme {
    mode: 'dark' | 'light';
    accent: string;
    colorTheme: string;
  }

  export function useAppApi(): AppApi;
  export function useAppEvents(event: string, callback: (data: unknown) => void): void;
  export function useTheme(): AppTheme;
  export function useAppInfo(): AppInfo;
  export function useNavigate(): (path: string) => void;
  export function useNotify(): (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void;
  export function useNavBadge(): (count: number) => void;
}

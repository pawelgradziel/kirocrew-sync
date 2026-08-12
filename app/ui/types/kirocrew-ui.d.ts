// Ambient type declarations for @kirocrew/app-sdk/ui — the host's shared
// component library (see website/src/kirocrew-ui/index.ts and
// website/src/components/ui.tsx in the kirocrew repo, and
// /vendor/kirocrew-ui.mjs in a running KiroCrew build's served dist). Editor/
// type-check convenience only: at runtime this specifier is resolved through
// the host's import map, never bundled (see ../esbuild.config.mjs).
//
// The module specifier is '@kirocrew/app-sdk/ui', NOT '@kirocrew/ui' —
// '@kirocrew/ui' is only the host's internal module name and is absent from
// the served import map, so importing it crashes at module resolution.
//
// Kept intentionally narrow — only what this app uses, and only names the
// vendor stub actually exports. Note SourceBadge is NOT in that stub's fixed
// destructure list (see /vendor/kirocrew-ui.mjs) and is deliberately omitted
// here even though the host's TS source defines it — importing it from this
// app would type-check but crash at runtime.
declare module '@kirocrew/app-sdk/ui' {
  import type { ComponentPropsWithoutRef, ReactNode } from 'react';

  export function Card(
    props: Omit<ComponentPropsWithoutRef<'div'>, 'dangerouslySetInnerHTML'>
  ): JSX.Element;

  export function CardTitle(
    props: Omit<ComponentPropsWithoutRef<'h3'>, 'dangerouslySetInnerHTML'>
  ): JSX.Element;

  export const Btn: React.ForwardRefExoticComponent<
    React.ButtonHTMLAttributes<HTMLButtonElement> & { danger?: boolean; primary?: boolean } & React.RefAttributes<HTMLButtonElement>
  >;

  export function SendBtn(
    props: { children: ReactNode } & Omit<ComponentPropsWithoutRef<'button'>, 'children' | 'dangerouslySetInnerHTML'>
  ): JSX.Element;

  export const Input: React.ForwardRefExoticComponent<
    React.InputHTMLAttributes<HTMLInputElement> & React.RefAttributes<HTMLInputElement>
  >;

  export function SearchInput(props: React.InputHTMLAttributes<HTMLInputElement>): JSX.Element;

  export function Badge(
    props: { variant: 'ok' | 'err' | 'warn' | 'aim' | 'muted' } & Omit<
      ComponentPropsWithoutRef<'span'>,
      'children' | 'dangerouslySetInnerHTML'
    > & { children: ReactNode }
  ): JSX.Element;

  export function StatCard(
    props: {
      label: string;
      value?: string | number | null;
      accent?: boolean;
      colorClass?: string;
      delay?: number;
      onClick?: () => void;
      active?: boolean;
      title?: string;
    } & Omit<ComponentPropsWithoutRef<'div'>, 'title' | 'onClick' | 'dangerouslySetInnerHTML'>
  ): JSX.Element;

  export function Skeleton(props: ComponentPropsWithoutRef<'div'>): JSX.Element;

  export function ContentSkeleton(props: { rows?: number }): JSX.Element;

  export function EmptyState(props: {
    icon: ReactNode;
    title: string;
    subtitle?: string;
    action?: ReactNode;
    testId?: string;
  }): JSX.Element;

  export function PageHeader(props: { title: ReactNode; subtitle?: string; actions?: ReactNode }): JSX.Element;

  export function Toggle(props: {
    checked: boolean;
    onChange: (v: boolean) => void;
    disabled?: boolean;
    label?: string;
    describedBy?: string;
    tone?: 'accent' | 'muted';
  }): JSX.Element;

  export function InfoTip(props: { text: string; placement?: 'auto' | 'top' }): JSX.Element;

  export interface Segment<T extends string = string> {
    key: T;
    label: string;
    icon?: ReactNode;
    count?: number;
    tooltip?: string;
    disabled?: boolean;
  }

  export function SegmentedControl<T extends string = string>(props: {
    segments: Segment<T>[];
    value: T;
    onChange: (value: T) => void;
    layoutId?: string;
    collapse?: boolean;
  }): JSX.Element;

  export function MarkdownRenderer(props: { children: string; className?: string }): JSX.Element;
}

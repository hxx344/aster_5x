import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Aster · 账户与交易管理',
  description: '子账户持仓、杠杆额度、风险与双向开仓执行看板。',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" className="dark">
      <body>{children}</body>
    </html>
  );
}

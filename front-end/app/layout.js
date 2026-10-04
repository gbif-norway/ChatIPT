import { Inter } from 'next/font/google'
import 'bootstrap/dist/css/bootstrap.min.css';
import 'bootstrap-icons/font/bootstrap-icons.css';
import './globals.css'
import './app.css'
import './conversion.css'
import './refresh.css'
import { AuthProvider } from './contexts/AuthContext'
import { ThemeProvider } from './contexts/ThemeContext'
import HeaderWrapper, { NavigationProvider } from './components/HeaderWrapper'

const inter = Inter({ subsets: ['latin'] })

export const metadata = {
  title: 'ChatIPT',
  description: 'Clean, standardise, and organise biodiversity data into DwC-DP and DwC-A publication packages',
  icons: {
    icon: [
      { url: '/images/chatipt-mark.svg', type: 'image/svg+xml' },
    ],
  },
}

export default function RootLayout({ children }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <link rel="icon" href="/images/chatipt-mark.svg" type="image/svg+xml" />
      </head>
      <body className={inter.className}>
        <ThemeProvider>
          <AuthProvider>
            <NavigationProvider>
              <HeaderWrapper />
              {children}
            </NavigationProvider>
          </AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  )
}

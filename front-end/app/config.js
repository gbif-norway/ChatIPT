const config = {
  baseApiUrl: process.env.NEXT_PUBLIC_BASE_API_URL,
};

// Debug logging
console.log('Config loaded with baseApiUrl:', config.baseApiUrl);
console.log('Environment variable NEXT_PUBLIC_BASE_API_URL:', process.env.NEXT_PUBLIC_BASE_API_URL);

export default config;

const webpack = require('webpack');

module.exports = {
  webpack: {
    configure: (webpackConfig) => {
      // webpackConfig.output.publicPath = './';
      // Ensure the target environment supports modern JavaScript
      webpackConfig.target = ['web', 'es2020'];
      // Add fallbacks for Node.js modules
      webpackConfig.resolve.fallback = {
        ...webpackConfig.resolve.fallback,
        "assert": require.resolve("assert"),
        "buffer": require.resolve("buffer"),
        "crypto": require.resolve("crypto-browserify"),
        "fs": false,
        "http": require.resolve("stream-http"),
        "https": require.resolve("https-browserify"),
        "os": require.resolve("os-browserify/browser"),
        "path": require.resolve("path-browserify"),
        "process": require.resolve("process/browser"),
        "stream": require.resolve("stream-browserify"),
        "url": require.resolve("url"),
        "util": require.resolve("util"),
        "vm": require.resolve("vm-browserify"),
        "zlib": require.resolve("browserify-zlib")
      };

      // Add plugins for polyfills
      webpackConfig.plugins = [
        ...webpackConfig.plugins,
        new webpack.ProvidePlugin({
          Buffer: ['buffer', 'Buffer'],
          process: 'process/browser',
        }),
      ];

      // Handle node: imports
      webpackConfig.resolve.alias = {
        ...webpackConfig.resolve.alias,
        'node:fs': false,
        'node:path': 'path-browserify',
        'node:crypto': 'crypto-browserify',
        'node:stream': 'stream-browserify',
        'node:buffer': 'buffer',
        'node:util': 'util',
        'node:url': 'url',
        'node:os': 'os-browserify/browser',
        'node:process': 'process/browser'
      };

      // Ensure compatibility with dynamic imports
      webpackConfig.output.environment = {
        arrowFunction: false, // Disable arrow functions for older environments
        dynamicImport: true, // Enable dynamic imports
        module: false, // Ensure compatibility with CommonJS
      };

      // Ensure the target is modern browsers
      webpackConfig.target = ['web', 'es2020'];

      return webpackConfig;
    },
  },
};
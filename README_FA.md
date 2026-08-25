# پروژه APK اپ ماینکرفت

این پروژه برای ساخت APK با GitHub Actions آماده شده است و به کامپیوتر/Android Studio نیاز ندارد.

## فایل‌های پروژه

- `app/src/main/assets/www/index.html` — نسخه فعلی رابط کاربری با دانلودر Native متصل شده.
- `app/src/main/assets/www/images/` — تصاویر پروژه را اینجا قرار بده.
- `all_mod.js` عمداً داخل APK قرار داده نشده است؛ در `index.html` باید لینک Raw GitHub خودت را جایگزین کنی.

## لینک all_mod.js

در `index.html` این خط را پیدا کن:

```html
<script src="https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPOSITORY/main/all_mod.js"></script>
```

و آن را با Raw URL واقعی فایل `all_mod.js` خودت جایگزین کن.

## نام فایل دانلودی

سیستم برای هر نوع فایل پسوند پیش‌فرض دارد. برای کنترل دقیق‌تر، داخل هر آیتم `db` می‌توانی این فیلد را اضافه کنی:

```js
downloadFileName: "my_mod.mcaddon"
```

نمونه:

```js
{
  id: 1,
  type: "ADD-ON",
  title: "مود من",
  downloadLink: "https://example.com/file",
  downloadFileName: "my_mod.mcaddon"
}
```

لینک `downloadLink` باید در نهایت فایل واقعی را برای دانلود برگرداند؛ اگر لینک فقط صفحه HTML باشد، دانلودر همان صفحه را دانلود می‌کند.

## ساخت APK با GitHub Actions

پوشه پروژه را در یک Repository گیت‌هاب آپلود کن. فایل `.github/workflows/build-apk.yml` خودش APK را با GitHub Actions می‌سازد.

بعد از Build، از بخش Actions وارد اجرای موفق شو و Artifact با نام `TLP-Minecraft-APK` را دانلود کن.

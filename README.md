# Saleor Flat Tax Plugin

`saleor-flat-tax-plugin` is a small plugin to enable flat taxes in Saleor.

It's based out of Saleor's
[VatlayerPlugin](https://github.com/saleor/saleor/blob/main/saleor/plugins/vatlayer/plugin.py#L49). Using the same
calculations but modified to use the taxes configured through the plugin and not take Country into consideration.

_Disclaimer_: This project is not connected/endorsed by saleor's community

## Installation ##

Using pip:

```bash
pip install saleor-flat-tax-plugin
```

Once installed Saleor will automatically detect the plugin, and you'll be able to see it in Saleor's dashboard
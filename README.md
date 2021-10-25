# TadCoin Blockchain network

We've reached mainnet and GUI apps are available for download! 
## :arrow_right::arrow_right: [Download TadCoin here](https://github.com/Tad-Network/tad-blockchain/releases) :arrow_left::arrow_left:

Have questions, suggestions or just want to show off your TadCoins? [Join our Discord](https://discord.gg/mwZF9DX544)

**How is this fork different?**

* We support both OG and NFT plots and get you **FULL** reward. Yes, that's right, **full reward for any type of plots.**
* Get most of the space on the hard drive - we support k28+ plots.  
* It's Open Source and forked off Chia Network 1.2.2. You can always audit code by diff'ing it with Chia upstream
* You don't need goddamn IP addresses for the nodes. Tad's network discovery works.
     
## Tad is big

There are thousands of users and dozens projects are working with TadCoin. Some of them:

* Exchanges (buy and sell TadCoin): [forks.green](https://www.forks.green/) and [xchforks.com](https://xchforks.com/tad/)   
* Calculators: [chiaforkscalculator.com](https://chiaforkscalculator.com/tad)
* Blockchain explorers: [alltheblocks.net](https://alltheblocks.net/tad/) and [tad.posat.io](https://tad.posat.io/)
* Other: [forks.wiki](https://forum.forks.wiki/pages/tad/)

## Installation

Download Tad Builds for Windows, Linux and MacOS [from releases page]([Download TadCoin here](https://github.com/Tad-Network/tad-blockchain/releases))

### Command Line Installation

It's also possible to run Tad blockchain using CLI interface. Use following guide for Ubuntu / Debian system:

```shell
sudo apt-get update
sudo apt-get upgrade -y

# Install Git
sudo apt install git -y

# Checkout the source and install
git clone https://github.com/Tad-Network/tad-blockchain
cd tad-blockchain

sh install.sh

. ./activate
```

Initialise Tad:
```shell
tad init
```

Then, import existing Chia passphrase
```shell
tad keys add
```

## Join Tad Community!
   
Do you have question, want to offer help, hunting for bounty or just want to way hi? Join our ever growing community

* [Tad Discord](https://discord.gg/mwZF9DX544)
* [Twitter @TadCoin](https://twitter.com/TadCoin)
* [Reddit /r/TadCoin](https://www.reddit.com/r/TadCoin/)
* [TadCoin.xyz project website](https://tadcoin.xyz/)

                                                        
## Ports used by TadCoin

➡ Full Node: **4044** 

Other ports: 
- harvester: **4448**, RPC: **4458**
- farmer: **4447** , RPC: **4457**
- wallet: **4449**, RPC: **4456**
- timelord_launcher: **4050**
- timelord: **8446**
- node: **4044**, RPC: **4555**
- TAD daemon: **4400**

For unix system you can check if anything is using these ports with this handy command:
```
netstat -tnap | grep -e '4044|4448|4458|4447|4457|4050|8446|4044|4555'
```
                                                              
## Credit

Thanks Chia Blockchain team and awesome people participating in the network!
